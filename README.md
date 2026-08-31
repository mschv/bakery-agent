# 🥐 Bakery Partner Co-Pilot

An AI operations partner for a solo baker — built for the **All Things Agentic Hackathon**, **Collaborative Partner** track.

Most small bakers run their whole business from memory and Instagram DMs: what's in stock, what's expiring, who ordered what, what they're owed. This agent is a conversational partner that takes over that mental overhead — it remembers, tracks, plans, and asks the right clarifying questions instead of guessing, so the baker can just talk to it the way they'd talk to an assistant manager.

**Live demo:** `https://bakery-copilot-925693519234.us-central1.run.app` — deployed on Cloud Run, but requires Google authentication to load (see [Cloud Run access note](#cloud-run-access-note) below). Run it locally (instructions below) to try it yourself, or see the demo video.

**Architecture diagram:** https://claude.ai/code/artifact/442a9b43-db1d-4dd1-8fe4-e250cd9bf840

---

## Why "Collaborative Partner"

The track asks for an agent that "leads the way and takes notes... asks clarifying questions, guides the user step-by-step, and has a clear way to capture feedback, so it constantly adapts to the user's unique way of thinking." Concretely, this agent:

- **Asks before assuming** — e.g. it never invents an expiration date, never fabricates revenue from a list price, and won't act on something it read from a blurry photo without confirming first.
- **Resolves ambiguity by conversation, not by demanding technical detail** — the baker never has to know an internal ID; if two things match a name, the agent lists the options and asks.
- **Has a real memory bank** — `save_baker_preference` / `get_baker_preferences` persist standing rules ("don't bake tarts on rainy days") that reshape the agent's behavior in every future session, and `save_customer_note` does the same per-customer (allergies, habits).
- **Takes real action, not just chat** — 21 tool functions that actually mutate Firestore: inventory, recipes, orders, sales, expenses.

## Features

- **Inventory** — track ingredients, auto-deduct stock when a recipe is baked, restock, flag low-stock/expiring items, add brand-new ingredients/recipes on the fly.
- **Orders** — structured order lifecycle (`pending → fulfilled → paid`/`cancelled`), accounts receivable, disambiguation when multiple orders match a query.
- **Financials** — real weekly/monthly summaries (revenue vs. cost, cash-basis), pricing suggestions from ingredient cost + target margin, non-ingredient expense tracking.
- **Planning** — a shopping list generated from planned bakes, and a production plan built automatically from open orders.
- **Multimodal input** — type, speak (voice is transcribed via a one-shot Gemini call, then flows through the normal tool-calling session), or attach a photo/PDF of an order note or invoice directly into the conversation.
- **Live dashboard** — open orders, this week's money snapshot, and inventory alerts, reading Firestore directly (no LLM call needed for the dashboard refresh).

## Technologies Used

| Layer | Technology |
|---|---|
| LLM | **Gemini 3.5 Flash**, via Vertex AI |
| Agent framework | **google-genai SDK** (automatic function calling — 21 registered tools) |
| Cloud infrastructure | **Cloud Run** (deployment), **Cloud Firestore** (database) |
| Frontend | Streamlit |
| Auth | Application Default Credentials — no hardcoded keys anywhere, locally or in the container |
| Testing | pytest, 50 tests against an in-memory Firestore fake (no network calls, no GCP credentials needed to run the suite) |

## Other Data Sources

None beyond what the baker provides directly — Firestore is seeded with the baker's own ingredients/recipes (`seed.py`), and everything else (orders, sales, activity log, preferences) is created through conversation. No external APIs or third-party data sources are used.

---

## Architecture

See the full diagram: **https://claude.ai/code/artifact/442a9b43-db1d-4dd1-8fe4-e250cd9bf840**

In short: Streamlit (frontend) → `agent.py`'s persistent Gemini chat session (backend) → either Gemini 3.5 Flash decides what to do, or a tool function reads/writes Firestore. Gemini and Firestore never talk to each other directly — `agent.py` is the only thing that bridges both. Voice is transcribed via a separate one-shot Gemini call before rejoining the normal flow; the dashboard panel reads Firestore directly without going through Gemini at all.

## Cloud Run Access Note

This project is deployed under a Cornell-managed Google Cloud organization, which enforces a domain-restricted-sharing policy that blocks granting `allUsers` access to any Cloud Run service — this is an institutional security control, not a deployment issue. The service is genuinely live and working (verified with an authenticated request), but anonymous visitors will get a 403. Proof of the real deployment (Cloud Run console, revision history, logs) is in the demo video. To actually try the app, run it locally — see below.

---

## Setup & Spin-Up Instructions

### Prerequisites
- Python 3.9+
- A GCP project with **Firestore** and **Vertex AI** enabled
- A service account key with Firestore + Vertex AI access (or `gcloud auth application-default login`)

### Run locally

```bash
git clone <this-repo-url>
cd bakery-agent

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Point at your GCP credentials (never commit this file — it's gitignored)
export GOOGLE_APPLICATION_CREDENTIALS="$(pwd)/key.json"
# or: gcloud auth application-default login

# Optional: override the default project/model location
export GCP_PROJECT_ID="your-project-id"
export GCP_LOCATION="global"   # gemini-3.5-flash is only served on the global Vertex AI endpoint

# One-time: seed sample ingredients/recipes into Firestore
python seed.py

streamlit run app.py
```

Open the local URL Streamlit prints (defaults to `http://localhost:8501`).

### Run the tests

```bash
pytest
```

No GCP credentials needed — the suite runs against an in-memory Firestore fake (`tests/fake_firestore.py`), fully offline, in well under a second.

### Deploy to Cloud Run

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

gcloud iam service-accounts create bakery-copilot-run \
  --display-name="Bakery Copilot Cloud Run service account"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:bakery-copilot-run@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/datastore.user"
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:bakery-copilot-run@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

gcloud run deploy bakery-copilot \
  --source . \
  --region=us-central1 \
  --service-account=bakery-copilot-run@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars="GCP_PROJECT_ID=YOUR_PROJECT_ID,GCP_LOCATION=global" \
  --min-instances=0 \
  --max-instances=2 \
  --allow-unauthenticated
```

(Drop `--allow-unauthenticated` — or expect it to silently fail — if your project is under an org policy like the one described above; grant `roles/run.invoker` to specific accounts instead.)

---

## Project Structure

```
agent.py       # All tool functions, the Gemini chat session, system instructions
app.py         # Streamlit UI — chat panel + live operations dashboard
seed.py        # One-time Firestore seed data (sample ingredients/recipes)
tests/         # pytest suite against an in-memory Firestore fake
Dockerfile     # Container build for Cloud Run
requirements.txt
```

## Findings & Learnings

- **Model upgrades aren't just a name swap.** Moving from `gemini-2.5-flash` to `gemini-3.5-flash` broke silently on several tools — the newer model validates function-calling schemas more strictly, and rejected several bare `list` type hints (no item type) that 2.5 had tolerated. Also learned `gemini-3.5-flash` is only served on Vertex AI's `global` endpoint in this project, not regional ones.
- **Revenue double-counting is a real risk in any agent that can both "close an order" and "log a sale."** The fix wasn't a hard technical block (too easy to get false positives) but a clear split in the system instruction plus idempotency on the order-paid transition, so even if the model gets confused, the same order can't be paid twice.
- **The model will confidently do the wrong thing if instructions are ambiguous, not just when they're missing.** Asking for an ingredient's expiration date before restocking is correct behavior — but the same instruction, read too broadly, caused the model to hallucinate a "placeholder expiration date is required" rule when adding a brand-new ingredient, where no such requirement exists. Precision in scoping *which* tool a rule applies to mattered more than the rule's content.
- **Institutional GCP orgs can block public Cloud Run access by policy**, independent of anything in the deploy config — worth checking `constraints/iam.allowedPolicyMemberDomains` early if a public demo URL matters for your submission.
