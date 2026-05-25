# AimHarder API Pública 1.0.0 — referencia operativa CFMP

Fuente local completa: `docs/aimharder-public-api-1.0.0.txt`.
PDF original recibido por WhatsApp el 2026-05-25.

## Reglas críticas
- Base URL: `https://api.aimharder.com`.
- Autenticación: `Authorization: Bearer <access_token>` en todas las peticiones.
- También enviar `Content-Type: application/json`.
- **Muy importante:** la API solo admite **HTTP/1.1**. En cURL usar siempre `--http1.1`. Si el cliente negocia HTTP/2 puede devolver **403 Forbidden**, incluso con token válido.
- Tokens:
  - `Access Token`: corto, para peticiones.
  - `Refresh Token`: para renovar cuando la API devuelve **410**.
- Refresh endpoint: `GET /auth/tokens/refresh` con el refresh token como Bearer.
- Errores habituales: 400 malformado, 401 token inválido, 403 permisos/protocolo, 404 no encontrado, 410 access token caducado, 429 rate limit, 500 servidor.

## Paginación
- Endpoints listados soportan cursor recomendado: `?cursor=` primera llamada; luego usar `pagination.nextCursor` hasta `hasMore=false`.
- También pueden aceptar `page`, pero aparece como obsoleto en varios endpoints desde junio 2026.
- Filtros opcionales en muchos listados: `id_from`, `id_to`.
- Respuesta típica: `data`, `pagination`, `info`.

## Endpoints relevantes para Metro Risk

### Clientes activos
`GET /clients`
- Lista clientes de alta del centro.
- Campos útiles:
  - `id` / `Id`
  - `name`, `first_surname`, `second_surname`
  - `mobile_number`, `email`
  - `creation_date`
  - `deactivation_date`, `deactivation_reason`
  - `class_data.reservation_date` = fecha/hora de última reserva/clase
  - `class_data.class_name`, `class_id`, `schedule_id`
  - `custom_fields`
- Uso: base general de socios y cruce de datos.

### Cliente por ID
`GET /clients/:client_id`
- Detalle de un cliente.

### Clientes sin reserva desde fecha
`GET /clients/no-booking/:date`
- Fecha en formato `YYYY-MM-DD`.
- Devuelve clientes con datos de última reserva/clase.
- Es el endpoint recomendado para el módulo de **Inactividad 7+ días**, usando fecha = hoy - 7 días.
- Documentación indica paginación por `page`.

### Calendario
`GET /calendar/:date_str`
- Fecha `YYYY-MM-DD`.
- Devuelve clases del día con `schedule_id`, `time`, `name`, `duration`, `limit`, sala, coach, etc.

### Clases
- `GET /classes`
- `GET /classes/:class_id`
- `GET /classes/:class_id/schedule`

### Reservas
- `POST /classes/booking/guest` crea reserva para invitado. Devuelve `data.id` como booking ID.
- `POST /classes/booking/cancel` cancela reserva por `booking_id`.

### Citas / appointments
- `GET /classes/appointments`
- `GET /classes/appointments/:appointment_id`
- `POST /classes/appointment/guest`

### Invitados
- `GET /guests`
- `GET /guests/:guest_id`

### Instructores
- `GET /instructors`
- `GET /instructors/:instructor_id`

### Leads
- `GET /leads`

### Tarifas
- `GET /memberships`
- `GET /memberships/:membership_id`

### Salas
- `GET /trainingrooms`
- `GET /trainingrooms/:room_id`

## Aplicación directa al módulo Inactividad
- No usar `/clients` como única fuente si buscamos “más de 7 días sin venir”.
- Usar primero `/clients/no-booking/<YYYY-MM-DD>` con fecha de corte = hoy - 7 días.
- Mantener fallback a `/clients` solo si hace falta.
- Conservar `deactivation_date` como exclusión: si tiene baja, no es socio activo.
- Calcular `days_without_class` desde `class_data.reservation_date`.
- Tramos UI: `8-14`, `15-21`, `22-30`, `31+`, `Sin registro`.
- Para depurar incidencias con AimHarder, generar ejemplo cURL con `--http1.1` y sin credenciales.
