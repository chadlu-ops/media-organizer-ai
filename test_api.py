import urllib.request
try:
    with urllib.request.urlopen("http://localhost:8001/api/downloads/status") as response:
        print(response.read().decode())
except Exception as e:
    print(f"Error: {e}")
