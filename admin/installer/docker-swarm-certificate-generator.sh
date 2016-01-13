#!/bin/bash
# Generate certs for {Docker, Swarm} TLS and start Swarm Manager.
set -ex

: ${s3_bucket:?}

# Get expect to autofill openssl inputs
sudo apt-get install -y expect

PASSPHRASE=$(dd bs=18 count=1 if=/dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | fold -w 32 | head -n 1)

DOCKER_CERT_HOME='/root/.docker'
mkdir -p ${DOCKER_CERT_HOME}
cat > ${DOCKER_CERT_HOME}/passphrase.txt << EOF
${PASSPHRASE}
EOF

# Generate CA private and public keys
cat > ${DOCKER_CERT_HOME}/createca1.exp << EOF
#!/usr/bin/expect -f
set timeout -1
spawn openssl genrsa -passout stdin -aes256 -out ${DOCKER_CERT_HOME}/ca-key.pem 4096
match_max 100000
send -- "${PASSPHRASE}\r"
expect eof
EOF
chmod +x ${DOCKER_CERT_HOME}/createca1.exp
${DOCKER_CERT_HOME}/createca1.exp

cat > ${DOCKER_CERT_HOME}/createca2.exp << EOF
#!/usr/bin/expect -f
set timeout -1
spawn openssl req -new -x509 -days 365 -key ${DOCKER_CERT_HOME}/ca-key.pem -sha256 -out ${DOCKER_CERT_HOME}/ca.pem
match_max 100000
expect -exact "Enter pass phrase for ${DOCKER_CERT_HOME}/ca-key.pem:"
send -- "${PASSPHRASE}\r"
expect -exact "\r
You are about to be asked to enter information that will be incorporated\r
into your certificate request.\r
What you are about to enter is what is called a Distinguished Name or a DN.\r
There are quite a few fields but you can leave some blank\r
For some fields there will be a default value,\r
If you enter '.', the field will be left blank.\r
-----\r
Country Name (2 letter code) \[AU\]:"
send -- "US\r"
expect -exact "US\r
State or Province Name (full name) \[Some-State\]:"
send -- "CA\r"
expect -exact "CA\r
Locality Name (eg, city) \[\]:"
send -- "SF\r"
expect -exact "SF\r
Organization Name (eg, company) \[Internet Widgits Pty Ltd\]:"
send -- "ClusterHQ\r"
expect -exact "ClusterHQ\r
Organizational Unit Name (eg, section) \[\]:"
send -- "engineering\r"
expect -exact "engineering\r
Common Name (e.g. server FQDN or YOUR name) \[\]:"
send -- "tech\r"
expect -exact "tech\r
Email Address \[\]:"
send -- "\r"
expect eof
EOF
chmod +x ${DOCKER_CERT_HOME}/createca2.exp
${DOCKER_CERT_HOME}/createca2.exp

# Create swarm manager key and CSR.
openssl genrsa -out ${DOCKER_CERT_HOME}/swarm-manager-key.pem 4096
openssl req -subj "/CN=$(/usr/bin/ec2metadata --public-ipv4)" -sha256 -new -key ${DOCKER_CERT_HOME}/swarm-manager-key.pem -out ${DOCKER_CERT_HOME}/swarm-manager.csr

# Sign swarm manager public key with CA.
echo subjectAltName = IP:$(/usr/bin/ec2metadata --local-ipv4),IP:$(/usr/bin/ec2metadata --public-ipv4),IP:127.0.0.1 > ${DOCKER_CERT_HOME}/extfile.cnf
echo extendedKeyUsage = clientAuth,serverAuth >> extfile.cnf
cat > ${DOCKER_CERT_HOME}/createswarmmanager.exp << EOF
#!/usr/bin/expect -f
set timeout -1
spawn openssl x509 -req -days 365 -sha256 -in ${DOCKER_CERT_HOME}/swarm-manager.csr -CA ${DOCKER_CERT_HOME}/ca.pem -CAkey ${DOCKER_CERT_HOME}/ca-key.pem -CAcreateserial -out ${DOCKER_CERT_HOME}/swarm-manager-cert.pem -extfile ${DOCKER_CERT_HOME}/extfile.cnf
match_max 100000
expect -exact "Signature ok\r
subject=/CN=$(/usr/bin/ec2metadata --public-ipv4)\r
Getting CA Private Key\r
Enter pass phrase for ${DOCKER_CERT_HOME}/ca-key.pem:"
send -- "${PASSPHRASE}\r"
expect eof
EOF
chmod +x ${DOCKER_CERT_HOME}/createswarmmanager.exp
${DOCKER_CERT_HOME}/createswarmmanager.exp

# Create client key.
openssl genrsa -out ${DOCKER_CERT_HOME}/client-key.pem 4096
openssl req -subj '/CN=client' -new -key ${DOCKER_CERT_HOME}/client-key.pem -out ${DOCKER_CERT_HOME}/client.csr
echo extendedKeyUsage = clientAuth,serverAuth > ${DOCKER_CERT_HOME}/extfile.cnf
cat > ${DOCKER_CERT_HOME}/createclient.exp << EOF
#!/usr/bin/expect -f
set timeout -1
spawn openssl x509 -req -days 365 -sha256 -in ${DOCKER_CERT_HOME}/client.csr -CA ${DOCKER_CERT_HOME}/ca.pem -CAkey ${DOCKER_CERT_HOME}/ca-key.pem -CAcreateserial -out ${DOCKER_CERT_HOME}/client-cert.pem -extfile ${DOCKER_CERT_HOME}/extfile.cnf
match_max 100000
expect -exact "Signature ok\r
subject=/CN=client\r
Getting CA Private Key\r
Enter pass phrase for ${DOCKER_CERT_HOME}/ca-key.pem:"
send -- "${PASSPHRASE}\r"
expect eof
EOF
chmod +x ${DOCKER_CERT_HOME}/createclient.exp
${DOCKER_CERT_HOME}/createclient.exp

# Set permissions.
chmod -v 0400 ${DOCKER_CERT_HOME}/ca-key.pem
chmod -v 0444 ${DOCKER_CERT_HOME}/ca.pem

# Push CA certs to S3 bucket.
/usr/bin/s3cmd put --config=/root/.s3cfg ${DOCKER_CERT_HOME}/ca.pem s3://${s3_bucket}/docker-swarm-tls-config/ca.pem
/usr/bin/s3cmd put --config=/root/.s3cfg ${DOCKER_CERT_HOME}/ca-key.pem s3://${s3_bucket}/docker-swarm-tls-config/ca-key.pem
/usr/bin/s3cmd put --config=/root/.s3cfg ${DOCKER_CERT_HOME}/passphrase.txt s3://${s3_bucket}/docker-swarm-tls-config/passphrase.txt

# Push Client cert to S3 bucket
/usr/bin/s3cmd put --config=/root/.s3cfg ${DOCKER_CERT_HOME}/client-key.pem s3://${s3_bucket}/docker-swarm-tls-config/client-key.pem
/usr/bin/s3cmd put --config=/root/.s3cfg ${DOCKER_CERT_HOME}/client-cert.pem s3://${s3_bucket}/docker-swarm-tls-config/client-cert.pem

