# Payroll Checker

**Payroll Checker** (WebUI name: **LohnCheck**) is a self-hosted Docker application for plausibility checking German payroll statements.

> Status: **v0.1.0 – first deployable preview**. The application is designed to identify arithmetic and structural discrepancies. It is not a substitute for tax, legal, payroll or social-security advice.

## Features in v0.1.0

- FastAPI-based WebUI
- Manual payroll plausibility check
- Gross/net payout consistency checks
- Employee contribution plausibility checks for pension, unemployment, health and long-term care insurance
- Configurable health-insurance additional contribution
- PDF upload endpoint and local document storage
- SQLite persistence
- Health endpoint for Docker/Unraid
- Fully local processing; uploaded payroll files are not sent to a cloud service
- Docker and Unraid-ready layout

## Planned next steps

- Official BMF payroll-tax calculation engine by tax year
- Exact annual social-security parameters and assessment ceilings
- Tax classes, church tax, solidarity surcharge and allowances
- Shift/night/Sunday/public-holiday supplements
- Payroll-line / wage-type mapping
- PDF text extraction and automatic field assignment
- OCR fallback for scanned payroll statements
- Saved employee profiles and month-to-month comparison

## Docker

The image is built automatically by GitHub Actions and published as:

```text
ghcr.io/2crazytv/payroll-checker:latest
```

The application listens on container port `8788`.

### Example Docker run

```bash
docker run -d \
  --name=payroll-checker \
  -p 8790:8788 \
  -e TZ=Europe/Berlin \
  -e APP_DATA_DIR=/data \
  -v /mnt/user/appdata/payroll-checker:/data \
  --restart unless-stopped \
  ghcr.io/2crazytv/payroll-checker:latest
```

Open:

```text
http://UNRAID-IP:8790
```

Health check:

```text
http://UNRAID-IP:8790/health
```

## Persistent data

The default persistent directory inside the container is `/data`.

Recommended Unraid mapping:

```text
/mnt/user/appdata/payroll-checker -> /data
```

The application creates:

```text
/data/payroll_checker.sqlite3
/data/documents/
```

## Unraid

An XML template is included at:

```text
unraid/payroll-checker.xml
```

Recommended host port: `8790`  
Container port: `8788`

## Privacy

Payroll documents contain highly sensitive personal information. Payroll Checker is therefore designed for local/self-hosted use. The application does not require an external API for its core functionality.

## Disclaimer

Results are plausibility checks only. Payroll rules in Germany depend on tax year, individual tax characteristics, health insurer, federal state, children, allowances, employment type, collective agreements, company agreements and other factors. A discrepancy detected by the application should be verified against the original payroll rules or by a qualified payroll/tax professional.
