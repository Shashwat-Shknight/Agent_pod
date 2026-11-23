# Deploy NuuCare (React version) on AWS, Free plan

**Goal:** one small Linux server on AWS runs the same single process you run locally
(`uvicorn app.main:app`). Nginx sits in front of it to handle HTTPS. Snowflake and the LLM stay where they are.

```
Browser ──HTTPS──▶ EC2 (Ubuntu): nginx :443 ──▶ uvicorn 127.0.0.1:8000 (API + React UI)
                                                   ├──▶ Snowflake (your account, key-pair auth)
                                                   └──▶ LLM API (Coforge or Groq, from .env)
```

**Why EC2 and not Lambda, App Runner or Elastic Beanstalk?**
- Sign-in sessions and scribe drafts live in the server's memory.
- AI calls can take up to 5 minutes.
- The Python dependencies are large.

One always-on virtual machine fits all three, and it is the option the Free plan covers.

## Before you start: know the Free plan rules (accounts created after 15 July 2025)

- **What you get:** $100 in credits at sign-up, plus up to $100 more for trying services. Usage is taken from
  the credits, not charged to your card.
- **When it ends:** after **6 months** or when the credits run out, whichever comes first. AWS then closes the
  account. You have 90 days to upgrade before AWS deletes everything.
- **What it costs us:** one `t3.micro` (or `t4g.micro`) running 24×7 with a public IP is roughly **$10–12 a
  month of credits**. Stopping the server when not demoing roughly halves that.
- **Synthetic data only.** No real patient data on this account. Real health data on AWS needs a signed
  Business Associate Addendum (BAA) and a paid account.

## Steps

### 1. Account safety (10 min)
1. Create the AWS account and choose the **Free plan**.
2. Turn on **MFA** for the root user. After that, do the daily work as an IAM user that also has MFA.
3. Pick one region and stay in it, e.g. **Asia Pacific (Mumbai) ap-south-1**.
4. Go to **Billing → Budgets** and create a **$5 monthly cost budget** with an email alert.
   Credits hide costs from a $0 budget, so set the alert on actual spend before credits.

### 2. Launch the server (10 min)
1. Go to **EC2 → Launch instance**.
   - **Name:** `nuucare-poc`
   - **Image:** Ubuntu Server 24.04 LTS (it comes with Python 3.12)
   - **Type:** one of the Free-tier-eligible micro types offered in your region (`t3.micro` or `t4g.micro`)
   - **Key pair:** create one, download the `.pem` file and keep it private
   - **Storage:** 20 GB gp3
2. Create a security group with these inbound rules:
   - **SSH 22:** *My IP* only
   - **HTTP 80** and **HTTPS 443:** your office or home IPs. Use *Anywhere* only if outside reviewers need access.
3. Allocate an **Elastic IP** and associate it with the instance, so the address survives restarts.

### 3. Prepare the server (15 min)
Connect with `ssh -i nuucare.pem ubuntu@<elastic-ip>`, then:
```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3-venv python3-pip git nginx
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab        # 1 GB RAM + 2 GB swap
git clone -b react-ui https://github.com/<you>/Agent_pod.git
cd Agent_pod/Clinical_decision_support_react
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 4. Secrets (5 min). Copy them over; never commit them
Run this from your laptop:
```powershell
scp -i nuucare.pem .env ubuntu@<elastic-ip>:~/Agent_pod/Clinical_decision_support_react/.env
ssh -i nuucare.pem ubuntu@<elastic-ip> "mkdir -p ~/.snowflake/keys"
scp -i nuucare.pem $HOME\.snowflake\keys\<key>.p8 ubuntu@<elastic-ip>:~/.snowflake/keys/
```
Then, on the server:
1. Run `chmod 600 .env ~/.snowflake/keys/*.p8`.
2. In `.env`, change `SNOWFLAKE_PRIVATE_KEY_PATH` to `/home/ubuntu/.snowflake/keys/<key>.p8`.
3. Check the connection: `.venv/bin/python -m app.db.setup check`.

If you are not using Snowflake, set `STORAGE_BACKEND=json` instead. Data then lives on the server's disk, so
take an EBS snapshot to back it up.

### 5. Run it as a service (5 min)
Create `/etc/systemd/system/nuucare.service`:
```ini
[Unit]
Description=NuuCare API + React UI
After=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/Agent_pod/Clinical_decision_support_react
ExecStart=/home/ubuntu/Agent_pod/Clinical_decision_support_react/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=always

[Install]
WantedBy=multi-user.target
```
Then start it:
```bash
sudo systemctl daemon-reload && sudo systemctl enable --now nuucare
curl -s localhost:8000/health
```

There must be **exactly one worker**: sessions are kept in memory, so a second worker would sign people out at
random. The `--proxy-headers` flag lets the app see HTTPS, which makes it mark the session cookie *Secure*.

### 6. Nginx and HTTPS (15 min)
1. HTTPS needs a domain name. Use a company subdomain, or a free one from a dynamic-DNS service, pointed at
   the Elastic IP.
2. Create `/etc/nginx/sites-available/nuucare`:
   ```nginx
   server {
       listen 80;
       server_name <your-domain>;
       client_max_body_size 30m;          # audio uploads up to 25 MB
       location / {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_read_timeout 300s;        # AI calls can take minutes
       }
   }
   ```
3. Enable the site and add the certificate:
   ```bash
   sudo ln -s /etc/nginx/sites-available/nuucare /etc/nginx/sites-enabled/ && sudo rm /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   sudo apt -y install certbot python3-certbot-nginx && sudo certbot --nginx -d <your-domain>
   ```

HTTPS is not optional:
- Browsers allow the **microphone** only on HTTPS or localhost.
- Without HTTPS, passwords would cross the network in plain text.

### 7. First sign-in and demo data (10 min)
1. Open `https://<your-domain>`. If the database already has users, sign in as usual. On an empty system,
   create the first Super Admin.
2. Optional: load the demo data with
   `.venv/bin/python -m app.scripts.seed_synthetic --password <choose-one>`.

## Day-2 operations

| Task | Command |
|---|---|
| Deploy a new version | `cd ~/Agent_pod && git pull && sudo systemctl restart nuucare` (dist is prebuilt; no Node needed) |
| Logs | `journalctl -u nuucare -f` |
| Health | `curl https://<your-domain>/health` |
| Save credits | Stop the instance when not demoing (the Elastic IP and disk still cost a little) |
| Back up (JSON mode) | EC2 → Volumes → *Create snapshot* |
| Tear down | Terminate the instance, release the Elastic IP, delete snapshots |

## Known limits of this setup (fine for a POC)

- **One server, no failover.** If it reboots, everyone signs in again.
- **Sessions in memory.** Moving to several servers would need a shared session store (e.g. Redis).
- **1 GB of RAM.** Swap covers the code tables. Use `t3.small` if the app feels slow (it costs more credits).
