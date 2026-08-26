# Running the logger as a Windows service (NSSM)

The station PC is Windows, so run the logger as an auto-starting service with
[NSSM](https://nssm.cc/) (no more auto-login desktop hack like the old system).

## 1. Prepare
```
C:\aqms\logger\            <- copy the logger/ folder here (OUTSIDE OneDrive)
python -m venv C:\aqms\logger\.venv
C:\aqms\logger\.venv\Scripts\pip install -r C:\aqms\logger\requirements.txt
copy config.example.yaml config.yaml   & edit device IPs / station_id
copy .env.example .env                  & put the real MQTT secrets here
```

## 2. Install the service
```
nssm install AQMS-Logger "C:\aqms\logger\.venv\Scripts\python.exe" "-m" "aqms_logger.main" "--config" "C:\aqms\logger\config.yaml"
nssm set AQMS-Logger AppDirectory C:\aqms\logger
nssm set AQMS-Logger AppStdout C:\aqms\logger\logs\logger.out.log
nssm set AQMS-Logger AppStderr C:\aqms\logger\logs\logger.err.log
nssm set AQMS-Logger Start SERVICE_AUTO_START
nssm start AQMS-Logger
```

## 3. Verify
```
nssm status AQMS-Logger
type C:\aqms\logger\logs\logger.out.log      # should show "<ts>: N readings (M valid) -> SQLite"
```

## Notes
- Keep `C:\aqms\logger\` **outside** OneDrive so `.env`, `config.yaml`, and the SQLite
  buffer are not synced to the cloud.
- To test once without installing a service:
  `C:\aqms\logger\.venv\Scripts\python -m aqms_logger.main --config config.yaml --once`
