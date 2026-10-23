import secrets as token_generator

import runsite

new_token = token_generator.token_urlsafe(32)
runsite.secrets.set("INTERNAL_API_TOKEN", new_token)
result = runsite.secrets.apply()
if result.restarting:
    print("Token rotated; the service is restarting.")
else:
    print(f"Token saved; the service is {result.status} and picks it up on its next start.")
