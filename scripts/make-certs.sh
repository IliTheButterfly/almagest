#!/usr/bin/env bash
# A private CA and a dev server certificate, per ADR 0001.
#
# Why this exists: `getUserMedia` and `NDEFReader` are gated behind a browser secure
# context, so the camera and Web NFC are simply absent over plain http on a LAN
# address. `.lan` cannot get a publicly-trusted certificate, so the CA has to be ours
# and has to be trusted on every phone that scans or provisions.
#
# Everything lands in `certs/`, which is gitignored — these are real private keys.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p certs && cd certs

LAN="$(ip -4 -o addr show scope global | awk 'NR==1{split($4,a,"/"); print a[1]}')"
: "${ALMAGEST_HOST:=almagest.lan}"

if [ ! -f ca.key ]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout ca.key -out ca.crt -subj "/CN=Almagest local CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign"
  echo "created a CA — install certs/ca.crt on every phone that scans"
else
  echo "reusing the existing CA (delete certs/ca.key to start over)"
fi

cat > leaf.cnf <<EOF
[req]
distinguished_name = dn
[dn]
[ext]
subjectAltName = DNS:${ALMAGEST_HOST}, DNS:localhost, IP:127.0.0.1, IP:${LAN}
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
basicConstraints = CA:FALSE
EOF

openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
  -subj "/CN=${ALMAGEST_HOST}"
# 398 days: Safari and Chrome reject a server certificate with a longer lifetime.
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 398 -sha256 -extfile leaf.cnf -extensions ext
chmod 600 ca.key server.key
rm -f server.csr leaf.cnf

echo
echo "certificate covers: ${ALMAGEST_HOST}, localhost, 127.0.0.1, ${LAN}"
echo "the dev server picks it up automatically — https://${LAN}:5173"
