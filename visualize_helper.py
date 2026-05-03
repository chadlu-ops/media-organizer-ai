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
import socket
from pathlib import Path
from datetime import datetime
from PIL import Image as PILImage

PORT = 8888
# Global state for current serving directory
CURRENT_ROOT = Path.cwd()
SCRIPT_DIR = Path(__file__).parent.resolve()  # Fixed: always points to the app's own directory
IS_RUNNING = False
PROGRESS_LOG = []
SCHEMA_LOCK = threading.Lock()
SCHEMA_CACHE = {"data": None, "time": 0}
SCHEMA_CACHE_TTL = 2  # 2 second cache to prevent flood reloads
DOWNLOAD_IS_RUNNING = False
DOWNLOAD_LOG = []
DOWNLOAD_HISTORY_FILE = SCRIPT_DIR / "download_history.json"
SUBSCRIPTIONS_FILE = SCRIPT_DIR / "logs" / "subscriptions.json"
DOWNLOAD_SETTINGS_FILE = SCRIPT_DIR / "logs" / "download_settings.json"
MEDIA_METADATA_CACHE_FILE = SCRIPT_DIR / "logs" / "media_metadata_cache.json"
SLIDESHOW_SOURCES_FILE = SCRIPT_DIR / "logs" / "slideshow_sources.json"
SLIDESHOW_REGISTRY_FILE = SCRIPT_DIR / "logs" / "slideshow_registry.json"

# Phase 1: Registry for high-performance initialization
SCAN_REGISTRY = []
SCAN_STATUS = {"total": 0, "scanned": 0, "running": False}
SCAN_LOCK = threading.Lock()

# Load registry from disk on startup
if SLIDESHOW_REGISTRY_FILE.exists():
    try:
        with open(SLIDESHOW_REGISTRY_FILE, "r", encoding="utf-8") as f:
            SCAN_REGISTRY = json.load(f)
            print(f"[Slideshow] Loaded {len(SCAN_REGISTRY)} items from persistent registry.")
    except: pass

def get_all_lan_ips():
    """Get all local network IP addresses of this machine."""
    ips = []
    try:
        import socket
        # Get all interfaces
        hostname = socket.gethostname()
        addr_infos = socket.getaddrinfo(hostname, None)
        for info in addr_infos:
            ip = info[4][0]
            # Filter for IPv4 and non-loopback
            if "." in ip and not ip.startswith("127."):
                if ip not in ips:
                    ips.append(ip)
        
        # Secondary method to ensure we catch the primary interface
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('10.254.254.254', 1))
            primary = s.getsockname()[0]
            if primary not in ips:
                ips.insert(0, primary)
        finally:
            s.close()
    except Exception:
        pass
    return ips if ips else ['127.0.0.1']

def log_corrupted_media(file_path, error_msg):
    try:
        corrupt_log = SCRIPT_DIR / "logs" / "corrupted_media.log"
        corrupt_log.parent.mkdir(parents=True, exist_ok=True)
        with open(corrupt_log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {file_path} - {error_msg}\n")
    except:
        pass

def get_video_info(file_path):
    """Get width, height, and duration of a video using ffprobe, accounting for rotation."""
    try:
        cmd = [
            'ffprobe', 
            '-v', 'error', 
            '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,duration:stream_tags=rotate:stream_side_data=displaymatrix', 
            '-of', 'json', 
            str(file_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        
        if 'streams' in data and len(data['streams']) > 0:
            stream = data['streams'][0]
            width = int(stream.get('width', 0))
            height = int(stream.get('height', 0))
            
            # Duration can be in the stream or format
            duration_str = stream.get('duration') or data.get('format', {}).get('duration', '0')
            duration = float(duration_str)
            
            # Check for rotation in tags
            rotation = 0
            tags = stream.get('tags', {})
            if 'rotate' in tags:
                try: rotation = int(tags['rotate'])
                except: pass
            
            # Check for rotation in side_data (newer ffprobe)
            if not rotation:
                side_data = stream.get('side_data', [])
                for sd in side_data:
                    # 'side_data_type' vs 'rotation' field
                    if sd.get('side_data_type') == 'Display Matrix' or 'rotation' in sd:
                        try: rotation = int(sd.get('rotation', 0))
                        except: pass
            
            # Swap width/height if rotated 90 or 270 degrees
            if abs(rotation) in [90, 270]:
                width, height = height, width
                
            return {"width": width, "height": height, "duration": duration, "rotation": rotation}
    except Exception as e:
        print(f"[FFPROBE ERR] {file_path}: {e}")
        log_corrupted_media(file_path, f"FFPROBE ERR: {e}")
    return None

def get_image_info(file_path):
    """Get width, height, and ratio of an image using PIL."""
    try:
        with PILImage.open(file_path) as img:
            w, h = img.size
            return {'width': w, 'height': h, 'ratio': w/h if h > 0 else 1.0}
    except Exception as e:
        print(f"[PIL ERR] {file_path}: {e}")
        log_corrupted_media(file_path, f"PIL ERR: {e}")
        return None

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    address_family = socket.AF_INET  # Force IPv4 to avoid network resolution issues
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
        print(f" [INCOMING] Request from {self.client_address[0]} for {self.path}")
        if self.path == '/api/status':
            self.send_json(200, {
                "status": "connected",
                "root": str(CURRENT_ROOT),
                "cwd": os.getcwd(),
                "exists": CURRENT_ROOT.exists()
            })
        elif self.path == '/api/slideshow/sources':
            try:
                if SLIDESHOW_SOURCES_FILE.exists():
                    with open(SLIDESHOW_SOURCES_FILE, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.send_json(200, data)
                else:
                    self.send_json(200, [])
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif self.path.startswith('/api/fs/list'):
            try:
                parsed_url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(parsed_url.query)
                path_str = query.get('path', [''])[0]
                
                if not path_str:
                    # Windows Drive Listing
                    import string
                    from ctypes import windll
                    drives = []
                    bitmask = windll.kernel32.GetLogicalDrives()
                    for letter in string.ascii_uppercase:
                        if bitmask & 1:
                            drives.append(f"{letter}:/")
                        bitmask >>= 1
                    self.send_json(200, {"path": "", "folders": drives, "is_root": True})
                    return

                p = Path(path_str)
                if not p.exists() or not p.is_dir():
                    self.send_json(400, {"error": "Invalid path"})
                    return
                
                folders = []
                for child in p.iterdir():
                    try:
                        if child.is_dir() and not child.name.startswith('.'):
                            folders.append(str(child).replace('\\', '/'))
                    except: pass
                
                folders.sort()
                self.send_json(200, {
                    "path": str(p).replace('\\', '/'),
                    "parent": str(p.parent).replace('\\', '/') if p.parent != p else None,
                    "folders": folders,
                    "is_root": False
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif self.path.startswith('/_abs/'):
            try:
                encoded_abs_path = self.path[6:]
                # Split at ? to remove query params like ?retry=1
                clean_path = encoded_abs_path.split('?')[0]
                abs_path_str = urllib.parse.unquote(clean_path)
                
                if abs_path_str.startswith('/') and ':' in abs_path_str:
                    abs_path_str = abs_path_str[1:]
                
                full_path = Path(abs_path_str)
                if not (full_path.exists() and full_path.is_file()):
                    self.send_error(404, "File not found")
                    return

                content_type, _ = mimetypes.guess_type(str(full_path))
                if not content_type: content_type = 'application/octet-stream'
                file_size = full_path.stat().st_size

                # Handle Range Requests (Very important for Video)
                range_header = self.headers.get('Range')
                start_byte = 0
                end_byte = file_size - 1
                is_partial = False

                if range_header and range_header.startswith('bytes='):
                    try:
                        is_partial = True
                        range_str = range_header[6:]
                        if '-' in range_str:
                            s, e = range_str.split('-')
                            if s: start_byte = int(s)
                            if e: end_byte = int(e)
                        
                        if start_byte >= file_size:
                            self.send_response(416) # Range Not Satisfiable
                            self.end_headers()
                            return
                        
                        # Fix end_byte if it's beyond file size
                        if end_byte >= file_size:
                            end_byte = file_size - 1
                    except:
                        is_partial = False

                chunk_length = end_byte - start_byte + 1

                if is_partial:
                    self.send_response(206)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Range', f'bytes {start_byte}-{end_byte}/{file_size}')
                    self.send_header('Content-Length', str(chunk_length))
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(file_size))
                
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()

                # Stream only the requested chunk
                try:
                    with open(full_path, 'rb') as f:
                        if start_byte > 0: f.seek(start_byte)
                        remaining = chunk_length
                        while remaining > 0:
                            chunk_size = min(remaining, 64 * 1024)
                            data = f.read(chunk_size)
                            if not data: break
                            try:
                                self.wfile.write(data)
                            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                                break
                            remaining -= len(data)
                except Exception as stream_err:
                    print(f"  [STREAM ERR] {full_path.name}: {stream_err}")
            except Exception as e:
                print(f"  [HANDLER ERR] {e}")
                try: self.send_error(500, str(e))
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
                import traceback
                traceback.print_exc()
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
        elif self.path == '/api/downloads/status':
            self.send_json(200, {
                "running": DOWNLOAD_IS_RUNNING,
                "log": DOWNLOAD_LOG[-100:]
            })
        elif self.path == '/api/downloads/config':
            config_path = SCRIPT_DIR / "gallery-dl.conf"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    self.send_json(200, {"config": f.read()})
            else:
                self.send_json(200, {"config": ""})
        elif self.path == '/api/downloads/history':
            if DOWNLOAD_HISTORY_FILE.exists():
                with open(DOWNLOAD_HISTORY_FILE, "r", encoding="utf-8") as f:
                    try:
                        history = json.load(f)
                    except:
                        history = []
                    self.send_json(200, {"history": history})
            else:
                self.send_json(200, {"history": []})
        elif self.path == '/api/downloads/subscriptions':
            if SUBSCRIPTIONS_FILE.exists():
                with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
                    try:
                        subs = json.load(f)
                    except:
                        subs = []
                    self.send_json(200, {"subscriptions": subs})
            else:
                self.send_json(200, {"subscriptions": []})
        elif self.path == '/api/downloads/settings':
            if DOWNLOAD_SETTINGS_FILE.exists():
                with open(DOWNLOAD_SETTINGS_FILE, "r", encoding="utf-8") as f:
                    try:
                        settings = json.load(f)
                    except:
                        settings = {}
                    self.send_json(200, {"settings": settings})
            else:
                self.send_json(200, {"settings": {}})
        elif self.path == '/api/images/seed':
            with SCAN_LOCK:
                seed = SCAN_REGISTRY[:500]
                self.send_json(200, {"items": seed, "total": len(SCAN_REGISTRY), "status": SCAN_STATUS})
        elif self.path.startswith('/api/images/index'):
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            offset = int(params.get('offset', [0])[0])
            limit = int(params.get('limit', [500])[0])
            with SCAN_LOCK:
                items = SCAN_REGISTRY[offset : offset + limit]
                self.send_json(200, {"items": items, "total": len(SCAN_REGISTRY), "status": SCAN_STATUS})
        elif self.path == '/api/slideshow/sources':
            if SLIDESHOW_SOURCES_FILE.exists():
                with open(SLIDESHOW_SOURCES_FILE, "r", encoding="utf-8") as f:
                    try:
                        self.send_json(200, json.load(f))
                    except:
                        self.send_json(200, {"sources": []})
            else:
                self.send_json(200, {"sources": []})
        else:
            return super().do_GET()

    def do_POST(self):
        global CURRENT_ROOT, IS_RUNNING, PROGRESS_LOG, DOWNLOAD_IS_RUNNING, DOWNLOAD_LOG
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

        elif self.path == '/api/downloads/status':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                data = json.loads(post_data)
                if data.get('running') == False:
                    DOWNLOAD_IS_RUNNING = False
                    self.send_json(200, {"status": "stopping_requested"})
                else:
                    self.send_json(400, {"error": "Invalid request"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

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

        elif self.path == '/api/cleanup_for_root':
            # Targeted cleanup for a specific root path (used by batch queue)
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                target_root = Path(params.get('root', '').strip())
                
                if not target_root.exists() or not target_root.is_dir():
                    self.send_json(404, {"error": f"Directory not found: {target_root}"})
                    return
                
                deleted = []
                for root, dirs, files in os.walk(target_root, topdown=False):
                    for name in dirs:
                        dir_path = Path(root) / name
                        if name in ["Organized", "logs"]:
                            continue
                        try:
                            if not any(dir_path.iterdir()):
                                dir_path.rmdir()
                                rel_path = str(dir_path.relative_to(target_root))
                                deleted.append(rel_path)
                                print(f"[CLEANUP] Deleted empty folder: {rel_path}")
                        except Exception as e:
                            print(f"[CLEANUP ERR] Failed to delete {dir_path}: {e}")
                
                self.send_json(200, {"status": "success", "root": str(target_root), "count": len(deleted), "deleted": deleted})
            except Exception as e:
                print(f"[CLEANUP ERR] {e}")
                self.send_json(500, {"error": str(e)})

        elif self.path == '/api/purge_duplicates':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                target_root = Path(params.get('root', '').strip()) if params.get('root') else CURRENT_ROOT
                
                if not target_root.exists():
                    self.send_json(404, {"error": "Root directory not found"})
                    return
                
                # Look for duplicates/ under Organized/ or directly under root
                dupes_dir = target_root / "Organized" / "duplicates"
                if not dupes_dir.exists():
                    dupes_dir = target_root / "duplicates"
                
                if not dupes_dir.exists():
                    self.send_json(200, {"status": "success", "count": 0, "message": "No duplicates folder found"})
                    return
                
                deleted_files = []
                deleted_dirs = []
                
                # Delete all files inside duplicates/
                for item in dupes_dir.rglob("*"):
                    if item.is_file():
                        try:
                            size = item.stat().st_size
                            item.unlink()
                            rel = str(item.relative_to(dupes_dir))
                            deleted_files.append({"path": rel, "size": size})
                            print(f"[PURGE] Deleted: {rel}")
                        except Exception as e:
                            print(f"[PURGE ERR] {item}: {e}")
                
                # Clean up empty subdirectories inside duplicates/
                for root_d, dirs, files in os.walk(dupes_dir, topdown=False):
                    for name in dirs:
                        dir_path = Path(root_d) / name
                        try:
                            if not any(dir_path.iterdir()):
                                dir_path.rmdir()
                                deleted_dirs.append(str(dir_path.relative_to(dupes_dir)))
                        except:
                            pass
                
                total_size = sum(f["size"] for f in deleted_files)
                size_mb = round(total_size / (1024 * 1024), 2)
                
                self.send_json(200, {
                    "status": "success",
                    "files_deleted": len(deleted_files),
                    "dirs_deleted": len(deleted_dirs),
                    "size_freed_mb": size_mb,
                    "dupes_path": str(dupes_dir)
                })
            except Exception as e:
                print(f"[PURGE ERR] {e}")
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
        elif self.path == '/api/images/scan':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                folders = params.get('folders', [])
                recursive = params.get('recursive', True)
                include_videos = params.get('include_videos', False)
                max_video_duration = params.get('max_video_duration', 30)
                fast_load = params.get('fast_load', False)

                def run_background_scan():
                    global SCAN_REGISTRY, SCAN_STATUS
                    import time
                    with SCAN_LOCK:
                        if SCAN_STATUS["running"]: return
                        SCAN_STATUS["running"] = True
                        
                        # Filter the current registry to only keep items that are in the requested folders
                        # This ensures that unchecking a folder actually removes its items.
                        new_registry = []
                        normalized_folders = [str(Path(f).resolve()).replace('\\', '/') for f in folders]
                        for item in SCAN_REGISTRY:
                            item_path = item["path"]
                            if any(item_path.startswith(f) for f in normalized_folders):
                                new_registry.append(item)
                        
                        SCAN_REGISTRY = new_registry
                        SCAN_STATUS["scanned"] = 0
                        SCAN_STATUS["total"] = 0

                    if fast_load:
                        with SCAN_LOCK:
                            SCAN_STATUS["running"] = False
                        return

                    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
                    video_extensions = {'.mp4', '.mov', '.webm', '.mkv', '.m4v', '.avi'} if include_videos else set()
                    
                    cache = {}
                    if MEDIA_METADATA_CACHE_FILE.exists():
                        try:
                            with open(MEDIA_METADATA_CACHE_FILE, "r", encoding="utf-8") as f:
                                cache = json.load(f)
                        except: pass

                    from concurrent.futures import ThreadPoolExecutor
                    import os
                    
                    found_files = []
                    for folder_str in folders:
                        base_path = Path(folder_str).resolve()
                        if not base_path.exists(): continue
                        
                        if recursive:
                            for root, dirs, files in os.walk(base_path):
                                root_path = Path(root)
                                for file in files:
                                    if Path(file).suffix.lower() in (image_extensions | video_extensions):
                                        found_files.append(root_path / file)
                        else:
                            for p in base_path.iterdir():
                                if p.is_file() and p.suffix.lower() in (image_extensions | video_extensions):
                                    found_files.append(p)

                    # Quick filter: separate new files from cached ones
                    registry_lookup = {item["path"]: item for item in SCAN_REGISTRY}
                    new_found_paths = []
                    for p in found_files:
                        p_str = str(p).replace('\\', '/')
                        if p_str not in registry_lookup:
                            new_found_paths.append(p)
                    
                    # Update status for progress bar
                    with SCAN_LOCK:
                        SCAN_STATUS["total"] = len(new_found_paths)
                        SCAN_STATUS["scanned"] = 0

                    updated_cache = False

                    def process_file(p):
                        nonlocal updated_cache
                        p_str = str(p).replace('\\', '/')
                        ext = p.suffix.lower()
                        is_video = ext in video_extensions
                        
                        info = cache.get(p_str)
                        if not info or 'ratio' not in info:
                            if is_video:
                                info = get_video_info(p)
                            else:
                                info = get_image_info(p)
                            if info:
                                with SCAN_LOCK:
                                    cache[p_str] = info
                                    updated_cache = True
                        
                        if info:
                            item = {
                                "path": p_str,
                                "type": "video" if is_video else "image",
                                "ratio": info.get('ratio', 1.0),
                                "width": info.get('width', 0),
                                "height": info.get('height', 0),
                                "orientation": "landscape" if info.get('width', 0) > info.get('height', 0) else "portrait"
                            }
                            with SCAN_LOCK:
                                if p_str not in registry_lookup:
                                    SCAN_REGISTRY.append(item)
                                SCAN_STATUS["scanned"] += 1

                    # Process only NEW files in parallel
                    if new_found_paths:
                        with ThreadPoolExecutor(max_workers=8) as executor:
                            executor.map(process_file, new_found_paths)
                    
                    # Periodic save of the whole registry
                    try:
                        SLIDESHOW_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
                        with open(SLIDESHOW_REGISTRY_FILE, "w", encoding="utf-8") as f:
                            json.dump(SCAN_REGISTRY, f)
                    except: pass
                    
                    if updated_cache:
                        try:
                            MEDIA_METADATA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                            with open(MEDIA_METADATA_CACHE_FILE, "w", encoding="utf-8") as f:
                                json.dump(cache, f)
                        except: pass

                    with SCAN_LOCK:
                        SCAN_STATUS["running"] = False

                threading.Thread(target=run_background_scan, daemon=True).start()
                self.send_json(200, {"status": "scanning_started"})
            except Exception as e:
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

        elif self.path == '/api/slideshow/sources':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                data = json.loads(post_data)
                # Ensure logs directory exists
                SLIDESHOW_SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(SLIDESHOW_SOURCES_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
                self.send_json(200, {"status": "success"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif self.path == '/api/downloads/run':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                urls = params.get('urls', [])
                force = params.get('force', False)
                if not urls:
                    self.send_json(400, {"error": "No URLs provided"})
                    return
                
                # Check if gallery-dl is available
                # We'll use the venv's executive if it exists, otherwise assume global
                venv_python = SCRIPT_DIR / ".venv" / "Scripts" / "python.exe"
                if venv_python.exists():
                    gdl_cmd = [str(venv_python), "-m", "gallery_dl"]
                else:
                    gdl_cmd = ["gallery-dl"]

                DOWNLOAD_IS_RUNNING = True
                msg = "[GDL] Starting sequential download batch"
                if force:
                    msg += " (FORCE MODE ENABLED)"
                DOWNLOAD_LOG = [f"{msg} ({len(urls)} items)..."]
                
                def run_gdl():
                    global DOWNLOAD_IS_RUNNING, DOWNLOAD_LOG
                    import time
                    
                    try:
                        env = os.environ.copy()
                        env["PYTHONUNBUFFERED"] = "1"
                        
                        for idx, url in enumerate(urls):
                            if not DOWNLOAD_IS_RUNNING:
                                DOWNLOAD_LOG.append("[GDL] Batch process aborted by user.")
                                break
                                
                            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            DOWNLOAD_LOG.append(f"\n[GDL] [{idx+1}/{len(urls)}] Processing: {url}")
                            file_count = 0
                            
                            current_cmd = gdl_cmd + ["-c", "gallery-dl.conf"]
                            if force:
                                current_cmd += ["--download-archive", "", "-o", "skip=false"]
                            current_cmd.append(url)
                            
                            process = subprocess.Popen(
                                current_cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                bufsize=0,
                                env=env,
                                cwd=str(SCRIPT_DIR)
                            )
                            
                            line_buffer = b''
                            while True:
                                # Small check to see if we should kill the process mid-stream
                                if not DOWNLOAD_IS_RUNNING:
                                    process.terminate()
                                    DOWNLOAD_LOG.append(f"[GDL] Terminating current download...")
                                    break
                                    
                                chunk = process.stdout.read1(4096) if hasattr(process.stdout, 'read1') else process.stdout.read(1)
                                if not chunk: break
                                line_buffer += chunk
                                while b'\n' in line_buffer or b'\r' in line_buffer:
                                    n_pos = line_buffer.find(b'\n')
                                    r_pos = line_buffer.find(b'\r')
                                    split_pos = min(n_pos, r_pos) if n_pos != -1 and r_pos != -1 else (n_pos if r_pos == -1 else r_pos)
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
                                        DOWNLOAD_LOG.append(line)
                                        if not line.startswith('[') and ('.' in line or '/' in line or '\\' in line):
                                            file_count += 1
                            
                            process.wait()
                            
                            # Update History immediately for this link
                            try:
                                history = []
                                if DOWNLOAD_HISTORY_FILE.exists():
                                    with open(DOWNLOAD_HISTORY_FILE, "r", encoding="utf-8") as f:
                                        history = json.load(f)
                                
                                # Move to top or insert
                                normalized_current = sorted([url])
                                history = [h for h in history if sorted(h.get('urls', [])) != normalized_current]
                                
                                history.insert(0, {
                                    "timestamp": start_time,
                                    "urls": [url],
                                    "count": file_count,
                                    "status": "Finished" if process.returncode == 0 else f"Error ({process.returncode})"
                                })
                                history = history[:100]
                                with open(DOWNLOAD_HISTORY_FILE, "w", encoding="utf-8") as f:
                                    json.dump(history, f, indent=2)
                            except Exception as hist_e:
                                print(f"[GDL] History update error: {hist_e}")

                            if idx < len(urls) - 1 and DOWNLOAD_IS_RUNNING:
                                DOWNLOAD_LOG.append("[GDL] Cooling down for 5 seconds...")
                                time.sleep(5)
                                
                        DOWNLOAD_LOG.append("\n[GDL] Batch sequence complete.")

                    except Exception as e:
                        DOWNLOAD_LOG.append(f"[GDL] Fatal Error: {e}")
                    finally:
                        DOWNLOAD_IS_RUNNING = False

                threading.Thread(target=run_gdl).start()
                self.send_json(200, {"status": "launched"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif self.path == '/api/slideshow/all_files':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                folders = params.get('folders', [])
                single_folder = params.get('folder', '').strip()
                if not folders and single_folder:
                    folders = [single_folder]
                recursive = params.get('recursive', True)
                include_videos = params.get('include_videos', False)
                max_video_duration = params.get('max_video_duration', 30) # New param for V2
                
                if not folders:
                    self.send_json(400, {"error": "At least one folder path is required"})
                    return

                cache = {}
                if MEDIA_METADATA_CACHE_FILE.exists():
                    try:
                        with open(MEDIA_METADATA_CACHE_FILE, "r", encoding="utf-8") as f:
                            cache = json.load(f)
                    except: pass

                image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
                video_extensions = {'.mp4', '.mov', '.webm', '.mkv', '.m4v', '.avi'} if include_videos else set()
                all_found_media = []
                cache_updated = False
                
                for folder_str in folders:
                    if not folder_str: continue
                    base_path = Path(folder_str).resolve()
                    if not base_path.exists():
                        continue

                    pattern = "**/*" if recursive else "*"
                    for p in base_path.glob(pattern):
                        if not p.is_file():
                            continue
                            
                        ext = p.suffix.lower()
                        p_str = str(p).replace('\\', '/')
                        
                        if ext in image_extensions:
                            # Check cache for image ratio
                            info = cache.get(p_str)
                            if not info or 'ratio' not in info:
                                info = get_image_info(p)
                                if info:
                                    cache[p_str] = info
                                    cache_updated = True
                            
                            if info:
                                all_found_media.append({
                                    "path": p_str,
                                    "type": "image",
                                    "ratio": info.get('ratio', 1.0),
                                    "width": info.get('width', 0),
                                    "height": info.get('height', 0)
                                })
                        elif ext in video_extensions:
                            # Check cache for video info
                            info = cache.get(p_str)
                            if not info or 'duration' not in info:
                                info = get_video_info(p)
                                if info:
                                    cache[p_str] = info
                                    cache_updated = True
                            
                            if info:
                                duration = info.get('duration', 0)
                                if duration <= max_video_duration:
                                    all_found_media.append({
                                        "path": p_str,
                                        "type": "video",
                                        "ratio": (info.get('width', 1) / info.get('height', 1)) if info.get('height', 0) > 0 else 1.0,
                                        "duration": duration,
                                        "width": info.get('width', 0),
                                        "height": info.get('height', 0)
                                    })

                # Save cache if needed
                if cache_updated:
                    try:
                        MEDIA_METADATA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        with open(MEDIA_METADATA_CACHE_FILE, "w", encoding="utf-8") as f:
                            json.dump(cache, f, indent=2)
                    except: pass

                # Deduplicate based on path
                unique_media = []
                seen_paths = set()
                for m in all_found_media:
                    if m["path"] not in seen_paths:
                        unique_media.append(m)
                        seen_paths.add(m["path"])
                
                print(f"[API] Slideshow: Found {len(unique_media)} items across {len(folders)} sources")
                self.send_json(200, {"status": "success", "files": unique_media})

            except Exception as e:
                import traceback
                print(f"[API ERR] Slideshow: {e}")
                print(traceback.format_exc())
                self.send_json(500, {"error": str(e)})

            except Exception as e:
                import traceback
                print(f"[API ERR] Slideshow: {e}")
                print(traceback.format_exc())
                self.send_json(500, {"error": str(e)})

        elif self.path == '/api/downloads/update':
            try:
                DOWNLOAD_IS_RUNNING = True
                DOWNLOAD_LOG = ["[GDL] Checking for updates..."]
                
                venv_python = SCRIPT_DIR / ".venv" / "Scripts" / "python.exe"
                if venv_python.exists():
                    cmd = [str(venv_python), "-m", "pip", "install", "-U", "gallery-dl"]
                else:
                    cmd = [sys.executable, "-m", "pip", "install", "-U", "gallery-dl"]

                def run_update():
                    global DOWNLOAD_IS_RUNNING, DOWNLOAD_LOG
                    try:
                        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                        for line in process.stdout:
                            if line.strip():
                                DOWNLOAD_LOG.append(line.strip())
                        process.wait()
                        DOWNLOAD_LOG.append(f"[GDL] Update complete (Code: {process.returncode})")
                    except Exception as e:
                        DOWNLOAD_LOG.append(f"[GDL] Update error: {e}")
                    finally:
                        DOWNLOAD_IS_RUNNING = False

                threading.Thread(target=run_update).start()
                self.send_json(200, {"status": "launched"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif self.path == '/api/downloads/config':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                config_content = params.get('config', '')
                config_path = SCRIPT_DIR / "gallery-dl.conf"
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(config_content)
                self.send_json(200, {"status": "success"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif self.path == '/api/downloads/subscriptions':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                subs = params.get('subscriptions', [])
                SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
                    json.dump(subs, f, indent=4)
                self.send_json(200, {"status": "success"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})

        elif self.path == '/api/downloads/settings':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length) if content_length > 0 else b'{}'
            try:
                params = json.loads(post_data)
                settings = params.get('settings', {})
                DOWNLOAD_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(DOWNLOAD_SETTINGS_FILE, "w", encoding="utf-8") as f:
                    json.dump(settings, f, indent=4)
                self.send_json(200, {"status": "success"})
            except Exception as e:
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

def run_subscription_poller():
    import time
    while True:
        try:
            if SUBSCRIPTIONS_FILE.exists():
                with open(SUBSCRIPTIONS_FILE, "r", encoding="utf-8") as f:
                    try:
                        subscriptions = json.load(f)
                    except:
                        subscriptions = []
                
                updated = False
                now = time.time()
                for sub in subscriptions:
                    last_polled = sub.get("last_polled", 0)
                    interval_hours = sub.get("interval", 1)
                    interval_seconds = interval_hours * 3600
                    
                    if now - last_polled >= interval_seconds:
                        global DOWNLOAD_IS_RUNNING, DOWNLOAD_LOG
                        if DOWNLOAD_IS_RUNNING:
                            continue # Wait for next check if a download is active
                        
                        # Storage check
                        min_free_gb = 0
                        if DOWNLOAD_SETTINGS_FILE.exists():
                            try:
                                with open(DOWNLOAD_SETTINGS_FILE, "r", encoding="utf-8") as fs:
                                    min_free_gb = float(json.load(fs).get("min_free_gb", 0))
                            except:
                                pass
                        
                        target_dir = str(SCRIPT_DIR)
                        config_path = SCRIPT_DIR / "gallery-dl.conf"
                        if config_path.exists():
                            try:
                                with open(config_path, "r", encoding="utf-8") as fc:
                                    # Strip comments (lines starting with // or #) and simple regex to grab base-directory if json fails
                                    content = fc.read()
                                    try:
                                        c_json = json.loads(content)
                                        ext = c_json.get("extractor", {})
                                        base_dir = ext.get("base-directory")
                                        if base_dir: target_dir = base_dir
                                    except:
                                        # Very basic fallback string search if config has comments breaking json parser
                                        import re
                                        match = re.search(r'"base-directory"\s*:\s*"([^"]+)"', content)
                                        if match:
                                            target_dir = match.group(1)
                            except:
                                pass
                        
                        if min_free_gb > 0:
                            import shutil
                            try:
                                # Ensure the target directory exists before checking, or check its parent
                                check_path = Path(target_dir)
                                if not check_path.exists():
                                    check_path.mkdir(parents=True, exist_ok=True)
                                usage = shutil.disk_usage(str(check_path))
                                free_gb = usage.free / (1024**3)
                                if free_gb < min_free_gb:
                                    print(f"[AUTO-POLL] Skipping check. Drive has {free_gb:.1f}GB free (requires {min_free_gb}GB).")
                                    continue # Skip all polling this minute
                            except Exception as e:
                                print(f"[AUTO-POLL] Error checking disk usage for {target_dir}: {e}")

                        url = sub.get("url")
                        if url:
                            DOWNLOAD_IS_RUNNING = True
                            DOWNLOAD_LOG = [f"[AUTO-POLL] Starting background check for {url}..."]
                            
                            venv_python = SCRIPT_DIR / ".venv" / "Scripts" / "python.exe"
                            gdl_cmd = [str(venv_python), "-m", "gallery_dl"] if venv_python.exists() else ["gallery-dl"]
                            
                            cmd = gdl_cmd + ["-c", "gallery-dl.conf", url]
                            
                            env = os.environ.copy()
                            env["PYTHONUNBUFFERED"] = "1"
                            process = subprocess.Popen(
                                cmd,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                bufsize=0,
                                env=env,
                                cwd=str(SCRIPT_DIR)
                            )
                            
                            line_buffer = b''
                            while True:
                                if not DOWNLOAD_IS_RUNNING:
                                    process.terminate()
                                    DOWNLOAD_LOG.append(f"[AUTO-POLL] Terminating check...")
                                    break
                                chunk = process.stdout.read1(4096) if hasattr(process.stdout, 'read1') else process.stdout.read(1)
                                if not chunk: break
                                line_buffer += chunk
                                while b'\n' in line_buffer or b'\r' in line_buffer:
                                    n_pos = line_buffer.find(b'\n')
                                    r_pos = line_buffer.find(b'\r')
                                    split_pos = min(n_pos, r_pos) if n_pos != -1 and r_pos != -1 else (n_pos if r_pos == -1 else r_pos)
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
                                        DOWNLOAD_LOG.append(line)
                            
                            process.wait()
                            DOWNLOAD_LOG.append(f"[AUTO-POLL] Finished background check for {url}")
                            DOWNLOAD_IS_RUNNING = False
                            
                            sub["last_polled"] = time.time()
                            updated = True
                            
                if updated:
                    with open(SUBSCRIPTIONS_FILE, "w", encoding="utf-8") as f:
                        json.dump(subscriptions, f, indent=4)
                        
        except Exception as e:
            print(f"[POLLER ERR] {e}")
            
        time.sleep(60)

def run_server(initial_dir):
    global CURRENT_ROOT
    CURRENT_ROOT = initial_dir
    handler = WorkspaceHandler
    
    # Start background poller thread
    threading.Thread(target=run_subscription_poller, daemon=True).start()

    ThreadingTCPServer.allow_reuse_address = True
    # Explicitly bind to 0.0.0.0 to ensure network access on all interfaces
    with ThreadingTCPServer(("0.0.0.0", PORT), handler) as httpd:
        ips = get_all_lan_ips()
        print(f"\n[Media Visualizer Helper - NETWORK ENABLED]")
        print(f"------------------------------------------")
        print(f"Serving from: {CURRENT_ROOT}")
        print(f"Local Access:   http://localhost:{PORT}")
        for ip in ips:
            print(f"Network Access: http://{ip}:{PORT}")
        
        print(f"\n[Troubleshooting]")
        print(f"1. Ensure this PC's network is set to 'Private', not 'Public'.")
        print(f"2. Use the IP that matches your other computer's subnet (e.g. 192.168.x.x).")
        print(f"3. Check that your Firewall allows port {PORT}.")
        print(f"------------------------------------------\n")
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
