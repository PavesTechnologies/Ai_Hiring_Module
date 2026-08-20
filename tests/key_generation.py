#ENCRYPTION_KEY_OAUTH_TOKEN_V1

# from cryptography.fernet import Fernet
# print(Fernet.generate_key().decode())


#OAUTH_STATE_SIGNING_KEY

import secrets
print(secrets.token_urlsafe(32))