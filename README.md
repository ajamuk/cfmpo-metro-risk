# CFMP Metro Risk

Aplicación interna de CrossFit Metropolitano para:

- seguimiento operativo de lesionados;
- histórico de curados;
- detección de socios con +7 días sin venir mediante API pública de AimHarder;
- base futura para riesgo de bajas.

## Estructura

```text
dashboard/              Metro Risk Scanner web app
telegram-lesionados/    API/DB/sync de registros de lesionados
docs/                   documentación operativa sin secretos
```

## Seguridad

Este repo no debe contener:

- tokens de AimHarder;
- `.env` reales;
- service accounts de Google;
- bases SQLite reales;
- CSVs/reportes con datos personales;
- JSONL de registros reales.

Usar `.env.example` y `telegram-lesionados/config.example.json` como plantilla.

## Servicios principales en producción

- `crossfit-metropolitano-dashboard.service`
- `telegram-lesionados-submit.service`

## Endpoints internos relevantes

- `GET /api/latest`
- `POST /api/inactive-refresh`
- `GET /api/inactive-members`
- `POST /api/injury-followup-done`
- `POST /api/injury-followup-note`
- `POST /api/injury-followup-pending-response`
- `POST /api/injury-followup-remove`
- `POST /api/injury-details-update`
```
