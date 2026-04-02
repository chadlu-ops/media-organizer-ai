import http.server
import socketserver
import os
import sys
import json
import subprocess
import threading
import urllib.parse
import shutil
import mimetypes
from pathlib import Path

PORT = 8000
# Global state for current serving directory
CURRENT_ROOT = Path.cwd()
SCRIPT_DIR = Path(__file__).parent.resolve()  # Fixed: always points to the app's own directory
IS_RUNNING = False
PROGRESS_LOG = []
SCHEMA_LOCK = threading.Lock()
SCHEMA_CACHE = {"data": None, "time": 0}
SCHEMA_CACHE_TTL = 2  # 2 second cache to prevent flood reloads

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    def handle_error(self, request, client_address):
        # Silence harmless connection abortions/resets (WinError 10053/10054)
        exctype, value = sys.exc_info()[:2]
        if exctype in (ConnectionAbortedError, ConnectionResetError):
            return 
        super().handle_error(request, client_address)

class WorkspaceHandler(http.server.SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        return super().end_headers()

    def send_json(self, code, data):
        """Send a JSON response with proper Content-Length for HTTP/1.1."""
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        # Handle CORS preflight
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self):
        if self.path == '/api/status':
            self.send_json(200, {
                "status": "connected",
                "root": str(CURRENT_ROOT),
                "cwd": os.getcwd(),
                "exists": CURRENT_ROOT.exists()
            })
        elif self.path.startswith('/_abs/'):
            # Experimental: Serve absolute path for Virtual Mode (Dry Runs)
            # URL format: /_abs/C:/Users/...
            try:
                # 1. Extract the encoded path segment
                encoded_abs_path = self.path[6:]
                # 2. Decode it (e.g. %20 -> space, %3A -> :)
                abs_path_str = urllib.parse.unquote(urllib.parse.unquote(encoded_abs_path))
                # 3. Handle potential leading slash from URL resolving
                if abs_path_str.startswith('/') and ':' in abs_path_str:
                    abs_path_str = abs_path_str[1:]
                
                full_path = Path(abs_path_str)
                
                if full_path.exists() and full_path.is_file():
                    # Robust MIME type detection
                    content_type, _ = mimetypes.guess_type(str(full_path))
                    if not content_type:
                        content_type = 'application/octet-stream'

                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    # Add Content-Length for better browser progress tracking
                    self.send_header('Content-Length', str(full_path.stat().st_size))
                    self.end_headers()
                    
                    # Stream the file in chunks instead of reading into memory
                    try:
                        with open(full_path, 'rb') as f:
                            shutil.copyfileobj(f, self.wfile, length=64*1024) # 64KB buffer
                        print(f"  [STREAM] {full_path} ({content_type})")
                    except (ConnectionAbortedError, ConnectionResetError):
                        # Expected when browser cancels a request (e.g. on scroll)
                        pass
                else:
                    self.send_error(404, "File not found")
                    print(f"  [STREAM MISS] {full_path}")
            except Exception as e:
                print(f"  [STREAM ERR] {e}")
                try:
                    self.send_error(500, str(e))
                except: pass
        elif self.path == '/api/schema' or self.path.startswith('/api/schema?'):
            try:
                import time
                now = time.time()
                
                with SCHEMA_LOCK:
                    if SCHEMA_CACHE["data"] and (now - SCHEMA_CACHE["time"] < SCHEMA_CACHE_TTL):
                        self.send_json(200, SCHEMA_CACHE["data"])
                        return

                    import importlib
                    import media_organizer
                    importlib.reload(media_organizer)
                    
                    SCHEMA_CACHE["data"] = media_organizer.PARAM_SCHEMA
                    SCHEMA_CACHE["time"] = now
                    self.send_json(200, media_organizer.PARAM_SCHEMA)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif self.path == '/api/status_job' or self.path.startswith('/api/status_job?'):
            self.send_json(200, {"running": IS_RUNNING})
        elif self.path == '/api/progress' or self.path.startswith('/api/progress?'):
            self.send_json(200, {"running": IS_RUNNING, "log": PROGRESS_LOG[-100:]})
        elif self.path == '/api/latest_log' or self.path.startswith('/api/latest_log?'):
            try:
                log_dir = Path(__file__).parent / "logs"
                if not log_dir.exists():
                    self.send_json(404, {"error": "Logs directory not found"})
                    return

                # Find newest CSV
                logs = list(log_dir.glob("*.csv"))
                if not logs:
                    self.send_json(404, {"error": "No logs found"})
                    return

                newest_log = max(logs, key=lambda p: p.stat().st_mtime)
                self.send_json(200, {
                    "filename": newest_log.name,
                    "path": str(newest_log),
                    "mtime": newest_log.stat().st_mtime
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        else:
            return super().do_GET()

    def do_POST(self):
        global CURRENT_ROOT, IS_RUNNING, PROGRESS_LOG
        if self.path == '/api/set_root' or self.path.startswith('/api/set_root?'):
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data)
                new_path_str = data.get('path', '').strip()
                
                # Cleanup: remove prefixes like [DRY RUN] or [COPY] if they leaked in
                import re
                new_path_str = re.sub(r'^\[.*?\]\s*', '', new_path_str)
                
                # Normalize slashes for Windows/Linux
                new_path_str = new_path_str.replace('\\', '/')
                new_path = Path(new_path_str)
                
                print(f"\n[API] set_root RECV: '{new_path}'")
                
                if new_path.exists() and new_path.is_dir():
                    CURRENT_ROOT = new_path
                    print(f"[API] Workspace Root UPDATED -> {CURRENT_ROOT}")
                    self.send_json(200, {"status": "success", "new_root": str(CURRENT_ROOT)})
                else:
                    print(f"[API] REJECTED: Path '{new_path}' (Exist: {new_path.exists()}, Dir: {new_path.is_dir()})")
                    self.send_json(400, {
                        "error": "Invalid path", 
                        "path": str(new_path),
                        "exists": new_path.exists(),
                        "is_dir": new_path.is_dir()
                    })
            except Exception as e:
                print(f"[API] ERROR processing set_root: {e}")
                try:
                    self.send_json(500, {"error": str(e)})
                except: pass

        elif self.path == '/api/browse_logs':
            try:
                log_dir = SCRIPT_DIR / "logs"
                if not log_dir.exists():
                    log_dir.mkdir(parents=True, exist_ok=True)
                
                print(f"[API] Opening logs folder: {log_dir}")
                os.startfile(str(log_dir))
                self.send_json(200, {"status": "success"})
            except Exception as e:
                print(f"[API] ERROR opening logs folder: {e}")
                self.send_json(500, {"error": str(e)})
        
        elif self.path == '/api/run':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            
            try:
                params = json.loads(post_data)
                print(f"\n[API] Triggering Media Organizer with params: {params}")
                
                # Build command — auto-map all params from PARAM_SCHEMA
                cmd = [sys.executable, "-u", "media_organizer.py", "--root", str(CURRENT_ROOT)]
                import importlib
                import media_organizer
                importlib.reload(media_organizer)
                PARAM_SCHEMA = media_organizer.PARAM_SCHEMA

                for p in PARAM_SCHEMA:
                    val = params.get(p["key"])
                    if val is None:
                        continue
                    if p["type"] == "bool":
                        if val:
                            cmd.append(p["cli"])
                    else:
                        cmd.extend([p["cli"], str(val)])

                # Set running state BEFORE starting thread to avoid race condition
                # where the first progress poll arrives before the thread sets IS_RUNNING
                IS_RUNNING = True
                PROGRESS_LOG = []
                cmd_str = ' '.join(cmd)
                print(f"Executing: {cmd_str}")
                PROGRESS_LOG.append(f"[API] Launching: {cmd_str}")
                PROGRESS_LOG.append(f"[API] Working directory: {Path(__file__).parent}")
                PROGRESS_LOG.append("[API] Waiting for process to start...")

                def run_organizer():
                    global IS_RUNNING, PROGRESS_LOG
                    try:
                        # Fast unbuffered environment
                        env = os.environ.copy()
                        env["PYTHONUNBUFFERED"] = "1"
                        
                        # Run from script's own directory so media_organizer.py is found
                        script_dir = str(Path(__file__).parent)
                        
                        # Use Popen with binary mode for raw byte-level reading
                        # This avoids readline() blocking on tqdm's \r output
                        process = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            bufsize=0,
                            env=env,
                            cwd=script_dir
                        )
                        
                        PROGRESS_LOG.append("[API] Process started (PID: {})".format(process.pid))
                        
                        # Read output byte-by-byte, assembling lines on \n or \r
                        # This ensures tqdm progress bars and logging both stream correctly
                        line_buffer = b''
                        while True:
                            chunk = process.stdout.read1(4096) if hasattr(process.stdout, 'read1') else process.stdout.read(1)
                            if not chunk:
                                # Process has closed stdout
                                break
                            line_buffer += chunk
                            
                            # Split on both \n and \r to handle tqdm and normal logging
                            while b'\n' in line_buffer or b'\r' in line_buffer:
                                # Find the earliest line terminator
                                n_pos = line_buffer.find(b'\n')
                                r_pos = line_buffer.find(b'\r')
                                
                                if n_pos == -1:
                                    split_pos = r_pos
                                elif r_pos == -1:
                                    split_pos = n_pos
                                else:
                                    split_pos = min(n_pos, r_pos)
                                
                                raw_line = line_buffer[:split_pos]
                                # Skip \r\n as a single break
                                if split_pos + 1 < len(line_buffer) and line_buffer[split_pos:split_pos+2] == b'\r\n':
                                    line_buffer = line_buffer[split_pos+2:]
                                else:
                                    line_buffer = line_buffer[split_pos+1:]
                                
                                try:
                                    line = raw_line.decode('utf-8', errors='replace').strip()
                                except:
                                    line = str(raw_line)
                                
                                if line:
                                    PROGRESS_LOG.append(line)
                                    print(f"[PROCESS] {line}")
                        
                        # Flush any remaining content in buffer
                        if line_buffer:
                            try:
                                line = line_buffer.decode('utf-8', errors='replace').strip()
                            except:
                                line = str(line_buffer)
                            if line:
                                PROGRESS_LOG.append(line)
                                print(f"[PROCESS] {line}")
                        
                        process.wait()
                        
                        if process.returncode == 0:
                            PROGRESS_LOG.append("[API] Media Organizer finished successfully.")
                            print("[API] Media Organizer finished successfully.")
                        else:
                            PROGRESS_LOG.append(f"[API] Process failed with code {process.returncode}")
                            print(f"[API] Process failed with code {process.returncode}")
                    except Exception as e:
                        import traceback
                        err_msg = f"[API] Error running organizer: {e}"
                        print(err_msg)
                        print(traceback.format_exc())
                        PROGRESS_LOG.append(err_msg)
                        PROGRESS_LOG.append(traceback.format_exc())
                    finally:
                        IS_RUNNING = False
                threading.Thread(target=run_organizer).start()
                
                self.send_json(200, {"status": "launched", "command": " ".join(cmd)})
            except Exception as e:
                print(f"[API] Run Error: {e}")
                self.send_json(500, {"error": str(e)})
        
        elif self.path == '/api/cleanup_empty_dirs':
            try:
                if not CURRENT_ROOT.exists():
                    self.send_json(404, {"error": "Root directory not found"})
                    return
                
                deleted = []
                # Traverse bottom-up to catch nested empty folders
                # Avoid deleting "Organized" or "logs" if they happen to be empty
                for root, dirs, files in os.walk(CURRENT_ROOT, topdown=False):
                    for name in dirs:
                        dir_path = Path(root) / name
                        if name in ["Organized", "logs"]:
                            continue
                        
                        try:
                            # Use iterdir for speed and to avoid list overhead
                            if not any(dir_path.iterdir()):
                                dir_path.rmdir()
                                rel_path = str(dir_path.relative_to(CURRENT_ROOT))
                                deleted.append(rel_path)
                                print(f"[CLEANUP] Deleted empty folder: {rel_path}")
                        except Exception as e:
                            print(f"[CLEANUP ERR] Failed to delete {dir_path}: {e}")
                
                self.send_json(200, {"status": "success", "count": len(deleted), "deleted": deleted})
            except Exception as e:
                print(f"[CLEANUP ERR] {e}")
                self.send_json(500, {"error": str(e)})

        elif self.path == '/api/diagnose':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                target_folder = params.get('folder', '').strip()
                if not target_folder:
                    self.send_json(400, {"error": "Folder path is required"})
                    return
                
                # Resolve relative to CURRENT_ROOT or extract if absolute
                path = Path(target_folder)
                if not path.is_absolute():
                    path = CURRENT_ROOT / path
                
                if not path.exists() or not path.is_dir():
                    self.send_json(404, {"error": f"Directory not found: {path}"})
                    return

                print(f"[API] Running Diagnostic on: {path}")
                
                # Import diagnostic function directly for speed, or run as subprocess
                # Running as module is cleaner if possible
                import diagnostic_tool
                import importlib
                importlib.reload(diagnostic_tool)
                
                # Capture stdout to avoid cluttering or redirection issues if needed, 
                # but diagnostic_tool.run_diagnostic can be modified to return data
                # For now, let's wrap it in a way that captures the report
                
                # We'll use a temporary file for the result since run_diagnostic writes to JSON
                temp_report = SCRIPT_DIR / "temp_diag_report.json"
                diagnostic_tool.run_diagnostic(
                    path, 
                    temp_report,
                    temporal_weight=params.get('temporal_weight', 0.3),
                    filename_weight=params.get('filename_weight', 0.0),
                    color_weight=params.get('color_weight', 0.0),
                    hash_weight=params.get('hash_weight', 0.0)
                )
                
                if temp_report.exists():
                    with open(temp_report, 'r') as f:
                        report_data = json.load(f)
                    temp_report.unlink() # Cleanup
                    self.send_json(200, report_data)
                else:
                    self.send_json(500, {"error": "Diagnostic failed to produce report"})

            except Exception as e:
                print(f"[API] Diagnose Error: {e}")
                self.send_json(500, {"error": str(e)})
        elif self.path == '/api/commit_log':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                log_filename = params.get('filename', '').strip()
                if not log_filename:
                    self.send_json(400, {"error": "Log filename is required"})
                    return

                # Resolve the log path
                log_path = SCRIPT_DIR / "logs" / log_filename
                if not log_path.exists():
                    # Try as absolute path
                    log_path = Path(log_filename)
                
                if not log_path.exists():
                    self.send_json(404, {"error": f"Log file not found: {log_filename}"})
                    return

                print(f"\n[API] Commit from Log: {log_path}")
                
                cmd = [sys.executable, "-u", "media_organizer.py", "--commit-log", str(log_path)]
                
                IS_RUNNING = True
                PROGRESS_LOG = []
                cmd_str = ' '.join(cmd)
                print(f"Executing: {cmd_str}")
                PROGRESS_LOG.append(f"[API] Launching commit: {cmd_str}")
                PROGRESS_LOG.append(f"[API] Working directory: {Path(__file__).parent}")
                PROGRESS_LOG.append("[API] Validating file integrity...")

                def run_commit():
                    global IS_RUNNING, PROGRESS_LOG
                    try:
                        env = os.environ.copy()
                        env["PYTHONUNBUFFERED"] = "1"
                        script_dir = str(Path(__file__).parent)
                        
                        process = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            bufsize=0,
                            env=env,
                            cwd=script_dir
                        )
                        
                        PROGRESS_LOG.append("[API] Commit process started (PID: {})".format(process.pid))
                        
                        line_buffer = b''
                        while True:
                            chunk = process.stdout.read1(4096) if hasattr(process.stdout, 'read1') else process.stdout.read(1)
                            if not chunk:
                                break
                            line_buffer += chunk
                            
                            while b'\n' in line_buffer or b'\r' in line_buffer:
                                n_pos = line_buffer.find(b'\n')
                                r_pos = line_buffer.find(b'\r')
                                
                                if n_pos == -1:
                                    split_pos = r_pos
                                elif r_pos == -1:
                                    split_pos = n_pos
                                else:
                                    split_pos = min(n_pos, r_pos)
                                
                                raw_line = line_buffer[:split_pos]
                                if split_pos + 1 < len(line_buffer) and line_buffer[split_pos:split_pos+2] == b'\r\n':
                                    line_buffer = line_buffer[split_pos+2:]
                                else:
                                    line_buffer = line_buffer[split_pos+1:]
                                
                                try:
                                    line = raw_line.decode('utf-8', errors='replace').strip()
                                except:
                                    line = str(raw_line)
                                
                                if line:
                                    PROGRESS_LOG.append(line)
                                    print(f"[COMMIT] {line}")
                        
                        if line_buffer:
                            try:
                                line = line_buffer.decode('utf-8', errors='replace').strip()
                            except:
                                line = str(line_buffer)
                            if line:
                                PROGRESS_LOG.append(line)
                                print(f"[COMMIT] {line}")
                        
                        process.wait()
                        
                        if process.returncode == 0:
                            PROGRESS_LOG.append("[API] Commit completed successfully.")
                            print("[API] Commit completed successfully.")
                        else:
                            PROGRESS_LOG.append(f"[API] Commit failed with code {process.returncode}")
                            print(f"[API] Commit failed with code {process.returncode}")
                    except Exception as e:
                        import traceback
                        err_msg = f"[API] Error during commit: {e}"
                        print(err_msg)
                        print(traceback.format_exc())
                        PROGRESS_LOG.append(err_msg)
                        PROGRESS_LOG.append(traceback.format_exc())
                    finally:
                        IS_RUNNING = False
                
                threading.Thread(target=run_commit).start()
                self.send_json(200, {"status": "launched", "log_file": str(log_path)})
            except Exception as e:
                print(f"[API] Commit Error: {e}")
                self.send_json(500, {"error": str(e)})

        else:
            self.send_error(404, "Unknown API endpoint")

    def translate_path(self, path):
        # 1. Strip query and fragments
        path = path.split('?', 1)[0]
        path = path.split('#', 1)[0]
        
        # 2. Decode URL encoding (e.g. %20 -> space)
        clean_path = urllib.parse.unquote(urllib.parse.unquote(path))
        
        # 3. Strip leading slash and split into components
        parts = clean_path.lstrip('/').split('/')
        
        # 4. Filter out any directory traversal attempts
        parts = [p for p in parts if p and p != '..' and p != '.']
        
        if not parts:
            # Root request — serve from script's directory (index listing)
            return str(SCRIPT_DIR)
        
        # 5. Check script directory FIRST (for app files: html, js, css, etc.)
        script_path = SCRIPT_DIR.joinpath(*parts)
        if script_path.exists():
            if not path.startswith('/api/'):
                print(f"  [APP] {path} -> {script_path}")
            return str(script_path)
        
        # 6. Fall back to CURRENT_ROOT (for data: logs, media, etc.)
        root_path = CURRENT_ROOT.joinpath(*parts)
        if not path.startswith('/api/'):
            exists = "EXISTS" if root_path.exists() else "MISSING"
            print(f"  [{exists}] {path} -> {root_path}")
            
        return str(root_path)

def run_server(initial_dir):
    global CURRENT_ROOT
    CURRENT_ROOT = initial_dir
    handler = WorkspaceHandler
    
    ThreadingTCPServer.allow_reuse_address = True
    with ThreadingTCPServer(("", PORT), handler) as httpd:
        print(f"\n[Media Visualizer Helper - Multi-Threaded]")
        print(f"------------------------------------------")
        print(f"Serving from: {CURRENT_ROOT}")
        print(f"Local Server URL: http://localhost:{PORT}")
        print(f"Keep this window open while using the visualizer dashboard.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down server...")
        except Exception as e:
            print(f"\n[CRITICAL] Server crashed: {e}")
        finally:
            httpd.server_close()

if __name__ == "__main__":
    initial_dir = Path.cwd()
    if len(sys.argv) > 1:
        initial_dir = Path(sys.argv[1])
    else:
        # Default auto-detect
        if initial_dir.name == "Organized":
            pass
        elif (initial_dir / "Organized").exists():
            initial_dir = initial_dir / "Organized"
        
    if not initial_dir.exists():
        print(f"Warning: Initial directory '{initial_dir}' does not exist. Defaulting to current folder.")
        initial_dir = Path.cwd()

    run_server(initial_dir)
