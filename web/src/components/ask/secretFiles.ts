/** Which shared files start as SECRET in HQ's composer (#1786): key and certificate files. */
const KEY_EXT =
  /\.(p8|p12|pfx|pem|key|crt|cer|der|p7b|p7c|jks|keystore|mobileprovision|provisionprofile|ppk|asc|gpg|kdbx|ovpn)$/i;
const KEY_NAME = /^(id_(rsa|dsa|ecdsa|ed25519)|\.env(\..+)?)$/i;

export function secretByDefault(name: string): boolean {
  return KEY_EXT.test(name) || KEY_NAME.test(name);
}
