"""
Quick GCS upload test — verifies the chart pipeline works end-to-end.
Run: python test_chart.py
"""
SERVICE_ACCOUNT_FILE = "credentials.json"
GCS_BUCKET = "nifty-risk-chart-megho"
GCS_OBJECT = "test_chart.png"

# 1. Generate test image
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from io import BytesIO

fig, ax = plt.subplots(figsize=(10, 6))
ax.axhline(23500, color="red", lw=2, label="CMP: 23500")
ax.axhspan(23200, 23800, alpha=0.2, color="blue", label="Range")
ax.set_ylim(23000, 24000)
ax.set_title("GCS Upload Test")
ax.legend()
buf = BytesIO()
fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
plt.close(fig)
buf.seek(0)
image_bytes = buf.read()
print(f"1. Image generated: {len(image_bytes)} bytes")

# 2. Upload to GCS
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request as AuthRequest
import requests as req

creds = Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE,
    scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
)
creds.refresh(AuthRequest())

resp = req.post(
    f"https://storage.googleapis.com/upload/storage/v1/b/{GCS_BUCKET}/o",
    params={"uploadType": "media", "name": GCS_OBJECT},
    headers={"Authorization": f"Bearer {creds.token}",
             "Content-Type": "image/png"},
    data=image_bytes,
)
print(f"2. Upload response: {resp.status_code}")
if resp.status_code >= 400:
    print(f"   ERROR: {resp.text}")
else:
    print(f"   OK")

# 3. Verify public URL serves raw image
url = f"https://storage.googleapis.com/{GCS_BUCKET}/{GCS_OBJECT}"
r = req.get(url, timeout=10)
ct = r.headers.get("Content-Type", "")
print(f"3. Public URL test: {r.status_code} | Content-Type: {ct} | {len(r.content)} bytes")
if ct.startswith("image/"):
    print(f"   =IMAGE(\"{url}\") WILL WORK")
else:
    print(f"   PROBLEM — not serving image bytes")

# 4. Cleanup
req.delete(
    f"https://storage.googleapis.com/storage/v1/b/{GCS_BUCKET}/o/{GCS_OBJECT}",
    headers={"Authorization": f"Bearer {creds.token}"},
)
print("4. Test file cleaned up")
