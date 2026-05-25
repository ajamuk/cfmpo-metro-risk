# Despliegue en VPS

## 1. Copiar app al VPS

Ruta recomendada:

```bash
/opt/crossfit-metropolitano-dashboard
```

## 2. Configurar `.env`

En produccion usa:

```env
DASHBOARD_HOST=127.0.0.1
DASHBOARD_PORT=8787
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=<password_larga>
```

Mantener los tokens de AimHarder solo en `.env`.

## 3. Servicio systemd

```bash
sudo cp deploy/crossfit-metropolitano-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable crossfit-metropolitano-dashboard
sudo systemctl restart crossfit-metropolitano-dashboard
```

## 4. Nginx

```bash
sudo cp deploy/nginx-crossfit-metropolitano.conf /etc/nginx/sites-available/crossfit-metropolitano-dashboard
sudo ln -s /etc/nginx/sites-available/crossfit-metropolitano-dashboard /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 5. HTTPS

Con Certbot:

```bash
sudo certbot --nginx -d bajas.crossfitmetropolitano.com
```

