# Set-and-Forget Monitor (Task Scheduler)

Run the Bull Put Spread monitor automatically every trading day from market open (9:30 AM ET) until market close (4:00 PM ET). No PowerShell commands needed after setup.

---

## One-time setup

### 1. Create your `.env` file

In the project root (`C:\Users\twluf\BullPutSpreadAnalyzer\bull-put-spread-analyzer`):

1. Copy `.env.example` to `.env`:
   ```powershell
   copy .env.example .env
   ```
2. Open `.env` in Notepad and fill in:
   - `TELEGRAM_BOT_TOKEN` – from BotFather
   - `TELEGRAM_CHAT_ID` – your chat ID
   - `SCHWAB_CLIENT_ID` – same as in Streamlit secrets
   - `SCHWAB_CLIENT_SECRET` – same as in Streamlit secrets
   - If you use Supabase: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
3. Save and close. Do **not** commit `.env` to git.

### 2. Install python-dotenv (if needed)

From the project root in PowerShell:

```powershell
cd C:\Users\twluf\BullPutSpreadAnalyzer\bull-put-spread-analyzer
pip install python-dotenv
```

### 3. Log in to Schwab once via the app

Open your Streamlit app, click **Connect to Schwab**, and log in. That creates `schwab_token.json` in the project root. The monitor uses this file.

### 4. Create the scheduled task in Windows

1. Press **Win + R**, type **`taskschd.msc`**, press Enter to open **Task Scheduler**.
2. Click **Create Basic Task** (right side or Action menu).
3. **Name:** `Bull Put Spread Monitor` (or any name). Click **Next**.
4. **Trigger:** **Daily**. Click **Next**.
5. **Start:** choose **9:30:00 AM**. Set **Recur every: 1 days**. Click **Next**.
6. **Action:** **Start a program**. Click **Next**.
7. **Program/script:** click **Browse** and select:
   ```
   C:\Users\twluf\BullPutSpreadAnalyzer\bull-put-spread-analyzer\run_monitor.bat
   ```
8. **Start in (optional):** enter:
   ```
   C:\Users\twluf\BullPutSpreadAnalyzer\bull-put-spread-analyzer
   ```
   (So the monitor’s working directory is the project root.)
9. Click **Next**, then **Finish**.
10. **Run only on weekdays:** Double-click the task → **Triggers** → **Edit**. Change to **Weekly**, select **Monday through Friday**, time **9:30:00 AM** → OK → OK.

---

## What happens every day

- **9:30 AM ET (weekdays):** Task Scheduler runs `run_monitor.bat`, which starts the monitor.
- The monitor reads `.env` and `schwab_token.json`, then every 5 minutes checks your saved trades and sends a Telegram alert if any trade hits **Close Now** or **Close Now or Roll**.
- **4:00 PM ET:** The monitor exits so it doesn’t run overnight.
- Next weekday at 9:30 AM the task runs again. No need to open PowerShell or the app.

---

## Verify it’s working

- **Task Scheduler:** Right-click your task → **Run**. Check **Last Run Result** (should be `0x0` = success). The monitor window may flash and close when it runs; that’s normal if you didn’t check “Run whether user is logged on or not.”
- **Telegram:** Trigger a **Close Now** condition on a test trade (or wait for a real one) and confirm you get an alert.
- **Logs:** To see monitor output, run it manually once from PowerShell:
  ```powershell
  cd C:\Users\twluf\BullPutSpreadAnalyzer\bull-put-spread-analyzer
  python -m mcps.monitor
  ```
  You should see “Bull Put Spread Monitor…” and then either checks or “Market closed” until 4 PM when it exits.

---

## If the project path is different

Edit `run_monitor.bat` and change the `cd /d "..."` line to your real project root path. Then the task will still run the same batch file.
