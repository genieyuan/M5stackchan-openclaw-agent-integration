#!/bin/bash
# Create a small private CA plus the two certificates for the edge <-> OpenClaw channel link (mutual TLS 1.3).
# usage: tools/make_pki.sh <channel-host-ip> [outdir]     e.g. tools/make_pki.sh 192.168.1.60 pki
# Keep ca.key offline once done. Nothing here is committed (pki/ is gitignored).
set -euo pipefail
IP=${1:?channel host IP}; OUT=${2:-pki}; mkdir -p "$OUT"; cd "$OUT"; umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout ca.key -out ca.crt -days 365 \
  -subj "/CN=StackChan household CA" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
mk() { openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -keyout "$1.key" -out "$1.csr" -subj "/CN=$2" 2>/dev/null
       printf "%b" "$3" > "$1.ext"; openssl x509 -req -in "$1.csr" -CA ca.crt -CAkey ca.key -CAcreateserial -out "$1.crt" -days 90 -extfile "$1.ext" 2>/dev/null; rm "$1.csr" "$1.ext"; }
mk channel stackchan-channel "subjectAltName=IP:$IP\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature\n"
mk edge stackchan-edge "subjectAltName=URI:urn:stackchan:edge\nextendedKeyUsage=clientAuth\nkeyUsage=critical,digitalSignature\n"
echo "wrote $OUT/{ca,channel,edge}.{crt,key} — copy ca.crt + channel.* to the OpenClaw host; edge.* stay on the edge"
