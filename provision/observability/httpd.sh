#!/usr/bin/env bash
set -euo pipefail

install -d -m 0755 /etc/labstack-httpd /srv/labstack-httpd
cat >/srv/labstack-httpd/index.html <<'EOF'
<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Labstack demo</title></head>
<body><h1>Labstack demo is healthy</h1><p>The HTTP service is available.</p></body>
</html>
EOF
chmod 0644 /srv/labstack-httpd/index.html
cat >/etc/labstack-httpd/httpd.conf <<'EOF'
ServerRoot /etc/labstack-httpd
ServerName localhost
Listen 8080
PidFile /run/labstack-httpd/httpd.pid
DefaultRuntimeDir /run/labstack-httpd
LoadModule mpm_event_module /usr/lib/apache2/modules/mod_mpm_event.so
LoadModule authz_core_module /usr/lib/apache2/modules/mod_authz_core.so
LoadModule dir_module /usr/lib/apache2/modules/mod_dir.so
DocumentRoot /srv/labstack-httpd
DirectoryIndex index.html
ServerTokens Prod
ServerSignature Off
<Directory /srv/labstack-httpd>
    Require all granted
    Options -Indexes
    AllowOverride None
</Directory>
ErrorLog /var/log/labstack-httpd/error.log
LogFormat "%h %l %u %t \"%r\" %>s %b" common
CustomLog /var/log/labstack-httpd/access.log common
EOF
cat >/etc/systemd/system/demo-httpd.service <<'EOF'
[Unit]
Description=Labstack demo HTTP server
After=network.target
[Service]
User=www-data
Group=www-data
RuntimeDirectory=labstack-httpd
LogsDirectory=labstack-httpd
ExecStartPre=/usr/sbin/apache2 -t -f /etc/labstack-httpd/httpd.conf
ExecStart=/usr/sbin/apache2 -f /etc/labstack-httpd/httpd.conf -DFOREGROUND
Restart=on-failure
NoNewPrivileges=true
CapabilityBoundingSet=
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/run/labstack-httpd /var/log/labstack-httpd
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable demo-httpd
systemctl restart demo-httpd
curl --fail --silent --show-error --retry 5 --retry-connrefused --retry-delay 1 \
  http://127.0.0.1:8080/ | grep -q 'Labstack demo is healthy'
