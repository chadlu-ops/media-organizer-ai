import urllib.request
import json
import time

server = "http://localhost:8001"

try:
    print("Triggering Download...")
    run_data = {"urls": ["https://httpbin.org/image/png"]} # Simple URL
    req = urllib.request.Request(f"{server}/api/downloads/run", 
                                 data=json.dumps(run_data).encode(),
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    
    with urllib.request.urlopen(req) as response:
        print(f"Run Result: {response.read().decode()}")

    print("\nWaiting 2 seconds for logs...")
    time.sleep(2)

    with urllib.request.urlopen(f"{server}/api/downloads/status") as response:
        print(f"Status/Logs: {response.read().decode()}")

except Exception as e:
    print(f"Error: {e}")
