# Prediccion de bajas - CrossFit MetroPolitano

Sistema diario para listar socios con riesgo de baja usando AimHarder y tus datos locales de pagos, tarifas y cancelaciones.

## Que hace

- Descarga clientes activos desde AimHarder.
- Usa la ultima reserva documentada por la API para medir inactividad.
- Cruza CSV locales de tarifas, pagos y cancelaciones.
- Calcula un score de 0 a 100 y clasifica el riesgo como Bajo, Medio o Alto.
- Genera cada dia un CSV y una vista HTML en `reports/`.

## Configuracion

1. Copia `.env.example` a `.env`.
2. Pega los tokens de AimHarder en `.env`.
3. Coloca tus ficheros en `data/` si los tienes:
   - `data/tarifas.csv`
   - `data/pagos.csv`
   - `data/cancelaciones.csv`

Importante: si algun token ha quedado escrito en un archivo compartido o en codigo, rota ese token desde AimHarder.

## Ejecutar

```bash
python3 run_daily.py
```

El resultado se guarda como:

- `reports/posibles_bajas_YYYY-MM-DD.csv`
- `reports/posibles_bajas_YYYY-MM-DD.html`

## Panel dashboard

Para abrir el panel web:

```bash
python3 dashboard_server.py
```

Despues entra en:

```text
http://127.0.0.1:8787
```

Tambien puedes abrir `start_dashboard.command` con doble clic en macOS.

## Criterios iniciales

El modelo suma riesgo por estas señales:

- Solicitud o registro de cancelacion.
- Muchos dias sin reservar clase.
- Alta reciente, sobre todo durante los primeros 3 meses.
- Media semanal de reservas baja.
- Pagos fallidos o devueltos en los ultimos 120 dias.
- Ultimo pago demasiado antiguo.
- Tarifas historicamente mas sensibles, como Academy o planes de pocas clases.

Estos pesos son editables en `churn_predictor/scoring.py`. Cuando tengamos datos historicos suficientes de bajas reales, el siguiente paso natural es entrenar un modelo estadistico y usar este scoring como baseline explicable.
