import requests
 
res = requests.get(
    "https://api.fingerprint.to/v1/linkedin/johndoe",
    headers={"Authorization": "Bearer fp_live_xxxxxxxxxxxxxxxx"},
)
 
profile = res.json()
print(profile["profileUrl"])