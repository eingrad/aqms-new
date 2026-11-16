# Running the logger as a Windows service (NSSM)

The station PC is Windows, so the logger runs as a Windows service with
[NSSM](https://nssm.cc/) (no auto-login desktop hack like the old system):

- starts at boot **without anyone logging in**
- restarts by itself if the logger crashes or exits
- keeps running when a user logs off
- comes back by itself after Windows Update reboots

## 1. Prepare (once)
```
C:\aqms\logger\            <- copy the logger/ folder here (OUTSIDE OneDrive)
python -m venv C:\aqms\logger\.venv
C:\aqms\logger\.venv\Scripts\pip install -r C:\aqms\logger\requirements.txt
copy config.example.yaml config.yaml   & edit device IPs / station_id
copy .env.example .env                  & put the real MQTT secrets here
mkdir C:\aqms\logger\logs
```
Download NSSM 2.24 from https://nssm.cc/download and copy `win64\nssm.exe` to `C:\aqms\nssm.exe`.

Test once before installing (stop any logger already running in a window first):
```
cd C:\aqms\logger
.venv\Scripts\python -m aqms_logger.main --config config.yaml --once --no-mqtt
```

## 2. Install the service (Command Prompt **as Administrator**)
```
set NSSM=C:\aqms\nssm.exe
%NSSM% install AQMS-Logger "C:\aqms\logger\.venv\Scripts\python.exe" "-m aqms_logger.main --config C:\aqms\logger\config.yaml"
%NSSM% set AQMS-Logger AppDirectory C:\aqms\logger
%NSSM% set AQMS-Logger DisplayName "AQMS Station Logger"
%NSSM% set AQMS-Logger Description "Fidas / FPI / GC -> SQLite -> MQTT"

rem start at boot, a little after the network is up
%NSSM% set AQMS-Logger Start SERVICE_DELAYED_AUTO_START

rem restart on any exit, after 10 s
%NSSM% set AQMS-Logger AppExit Default Restart
%NSSM% set AQMS-Logger AppRestartDelay 10000

rem graceful stop: Ctrl+C first, give it 10 s to close the analyzers / SQLite
%NSSM% set AQMS-Logger AppStopMethodConsole 10000

rem one log file, rotated at 10 MB so it never fills the disk
%NSSM% set AQMS-Logger AppStdout C:\aqms\logger\logs\logger.log
%NSSM% set AQMS-Logger AppStderr C:\aqms\logger\logs\logger.log
%NSSM% set AQMS-Logger AppRotateFiles 1
%NSSM% set AQMS-Logger AppRotateOnline 1
%NSSM% set AQMS-Logger AppRotateBytes 10485760

rem UTF-8 log output (the log lines contain characters like the arrow)
%NSSM% set AQMS-Logger AppEnvironmentExtra PYTHONUTF8=1

rem if NSSM itself ever dies, Windows restarts the service
sc failure AQMS-Logger reset= 86400 actions= restart/60000/restart/60000/restart/60000

%NSSM% start AQMS-Logger
```

## 3. Verify
```
cd C:\aqms\logger
.venv\Scripts\python -m aqms_logger.status --config config.yaml
```
One screen with the service state, the age of the last stored minute, the rows still
waiting for the server, and per instrument OK / CALIBRATING / NO ANSWER with its values.
It ends with `RESULT: OK` or a list of what needs attention (exit code 0 / 1).

Watch the log live (PowerShell):
```
Get-Content C:\aqms\logger\logs\logger.log -Wait -Tail 20
```
Then **reboot the PC and do not log in**: the dashboard should keep getting data.

Alerts by Telegram / email when the station or an instrument stops: the server's
`aqms-watchdog` ([../../watchdog/README.md](../../watchdog/README.md)).

## 4. The PC itself must come back after a power cut
A service only starts if Windows starts. On the station PC:
- **BIOS / UEFI**: set *Restore on AC Power Loss* (also called *AC Recovery* /
  *After Power Failure*) to **Power On**. Without this the PC stays off after a blackout.
- **Never sleep or hibernate** (Command Prompt as Administrator):
  ```
  powercfg /change standby-timeout-ac 0
  powercfg /change hibernate-timeout-ac 0
  ```
- A UPS is recommended; the SQLite buffer resends anything missed once MQTT is back.

## Day-to-day
```
%NSSM% restart AQMS-Logger      # after editing config.yaml or .env
%NSSM% stop AQMS-Logger         # before updating the code, then start again
%NSSM% edit AQMS-Logger         # GUI to view / change all the settings above
%NSSM% remove AQMS-Logger confirm
```

## Notes
- Keep `C:\aqms\logger\` **outside** OneDrive so `.env`, `config.yaml`, and the SQLite
  buffer are not synced to the cloud.
- The service runs as LocalSystem, which can reach the LAN analyzers; no password needed.
- Only one logger may run at a time: close any logger running in a console window
  before starting the service.
