import urllib.request
import json

server = "http://localhost:8001"

# Test Save Config
try:
    print("Testing Save Config...")
    config_data = {"config": '{"extractor": {"base-directory": "E:/image sorter/downloads/"}}'}
    req = urllib.request.Request(f"{server}/api/downloads/config", 
                                 data=json.dumps(config_data).encode(),
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urllib.request.urlopen(req) as response:
        print(f"Save Result: {response.read().decode()}")

    # Test Get Config
    print("\nTesting Get Config...")
    with urllib.request.urlopen(f"{server}/api/downloads/config") as response:
        print(f"Get Result: {response.read().decode()}")

except Exception as e:
    print(f"Error: {e}")
