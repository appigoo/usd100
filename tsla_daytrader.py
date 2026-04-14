import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timezone, timedelta
import time
import requests
from bs4 import BeautifulSoup
import re

st.set_page_config(
    page_title="TSLA/TSLL Day Trader – $100/day",
    page_icon="⚡",
    layout="wide"
)

st.markdown("""
<style>
  .signal-buy  { color: #00e676; font-size: 1.8rem; font-weight: 700; }
  .signal-sell { color: #ff1744; font-size: 1.8rem; font-weight: 700; }
  .signal-hold { color: #ffab00; font-size: 1.8rem; font-weight: 700; }
  .session-badge {
    display: inline-block; padding: 4px 14px;
    border-radius: 20px; font-size: 0.85rem; font-weight: 600;
  }
  .stAlert { border-radius: 10px; }
</style>
""", unsafe_allow_html=True)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TRADING SESSION DETECTOR
# ╚══════════════════════════════════════════════════════════════════════════════

def is_dst_us(dt_utc: datetime) -> bool:
    year = dt_utc.year
    mar = datetime(year, 3, 8, 7, 0, tzinfo=timezone.utc)
    mar += timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(year, 11, 1, 6, 0, tzinfo=timezone.utc)
    nov += timedelta(days=(6 - nov.weekday()) % 7)
    return mar <= dt_utc < nov

def get_et_time() -> datetime:
    now_utc = datetime.now(timezone.utc)
    offset  = timedelta(hours=-4) if is_dst_us(now_utc) else timedelta(hours=-5)
    return now_utc.astimezone(timezone(offset))

def get_trading_session() -> dict:
    et  = get_et_time()
    dow = et.weekday()   # 0=Mon … 6=Sun
    hm  = et.hour + et.minute / 60.0
    dst    = is_dst_us(datetime.now(timezone.utc))
    tz_str = "夏令时 EDT" if dst else "冬令时 EST"

    if dow == 5 or (dow == 6 and hm < 20.0):
        return dict(session="CLOSED", label="休市（周末）", color="#555",
                    use_scraper=False, et=et, tz=tz_str, dst=dst)
    if 4.0 <= hm < 9.5:
        return dict(session="PRE",    label="盘前交易 Pre-Market",  color="#7c4dff",
                    use_scraper=True,  et=et, tz=tz_str, dst=dst)
    if 9.5 <= hm < 16.0:
        return dict(session="REGULAR",label="正式交易 Regular",     color="#00e676",
                    use_scraper=False, et=et, tz=tz_str, dst=dst)
    if 16.0 <= hm < 20.0:
        return dict(session="POST",   label="盘后交易 After-Hours", color="#ff9100",
                    use_scraper=True,  et=et, tz=tz_str, dst=dst)
    if hm >= 20.0 or hm < 4.0:
        if dow in (0, 1, 2, 3, 6):
            return dict(session="NIGHT", label="夜盘交易 Night Session", color="#40c4ff",
                        use_scraper=True,  et=et, tz=tz_str, dst=dst)

    return dict(session="CLOSED", label="休市", color="#555",
                use_scraper=False, et=et, tz=tz_str, dst=dst)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SCRAPER — uk.finance.yahoo.com
# ╚══════════════════════════════════════════════════════════════════════════════

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://uk.finance.yahoo.com/",
}

def _get_yahoo_url(ticker: str) -> list[str]:
    """
    Return ordered list of URLs to try for a given ticker.
    - TSLA: UK Yahoo first (cleaner page), fallback to US Yahoo
    - TSLL: UK Yahoo doesn't carry this US-only leveraged ETF (404),
            so go straight to US Yahoo, with UK as fallback just in case.
    """
    uk = f"https://uk.finance.yahoo.com/quote/{ticker}/"
    us = f"https://finance.yahoo.com/quote/{ticker}/"
    if ticker.upper() == "TSLL":
        return [us, uk]
    return [uk, us]


@st.cache_data(ttl=30)
def scrape_uk_yahoo(ticker: str) -> dict:
    """
    Scrape Yahoo Finance quote page for extended-hours price.
    Tries URLs in order returned by _get_yahoo_url(); skips on 404 and tries next.

    Yahoo Finance page contains fin-streamer elements with data-field values:
      regularMarketPrice  — last regular session close
      preMarketPrice      — pre-market price
      postMarketPrice     — after-hours price
    """
    urls = _get_yahoo_url(ticker)
    url  = urls[0]   # will be updated if we fall back
    result = {
        "price": None,
        "regular_price": None,
        "pre_price": None,
        "post_price": None,
        "source": url,
        "raw_html_snippet": "",
        "error": None,
    }

    try:
        html = None
        for candidate_url in urls:
            try:
                resp = requests.get(candidate_url, headers=HEADERS, timeout=15)
                if resp.status_code == 404:
                    continue          # try next URL
                resp.raise_for_status()
                html = resp.text
                url  = candidate_url
                result["source"] = url
                break
            except requests.exceptions.HTTPError:
                continue              # try next URL on any HTTP error

        if html is None:
            result["error"] = f"所有 URL 均无法访问: {urls}"
            return result

        soup = BeautifulSoup(html, "html.parser")

        # ── Strategy 1: fin-streamer tags (most reliable on Yahoo Finance) ──
        # These are custom web-components Yahoo uses for live streaming data.
        # data-value holds the numeric value; data-field names the metric.
        for tag in soup.find_all("fin-streamer"):
            field = tag.get("data-field", "")
            raw   = tag.get("data-value") or tag.get_text(strip=True)
            try:
                val = float(str(raw).replace(",", ""))
            except (ValueError, TypeError):
                continue

            # Only accept values in a realistic stock-price range (1 – 99999)
            # to avoid capturing volume, market-cap, or percentage fields
            if not (1.0 <= val <= 99_999.0):
                continue

            if field == "regularMarketPrice":
                result["regular_price"] = val
            elif field == "preMarketPrice":
                result["pre_price"] = val
            elif field == "postMarketPrice":
                result["post_price"] = val

        # ── Strategy 2: data-testid attributes (newer Yahoo layout) ──
        # Yahoo sometimes uses data-testid="qsp-price" for the main quote price
        if result["regular_price"] is None:
            for tag in soup.find_all(attrs={"data-testid": re.compile(r"qsp-price|regularMarket", re.I)}):
                txt = tag.get_text(strip=True).replace(",", "")
                m = re.search(r"(\d{1,5}\.\d{1,4})", txt)
                if m:
                    try:
                        val = float(m.group(1))
                        if 1.0 <= val <= 99_999.0:
                            result["regular_price"] = val
                            break
                    except ValueError:
                        pass

        # ── Strategy 3: JSON-LD / meta tags as last resort ──
        # Yahoo embeds structured data in <script type="application/ld+json">
        if result["regular_price"] is None:
            for script in soup.find_all("script", type="application/ld+json"):
                txt = script.string or ""
                m = re.search(r'"price"\s*:\s*"?([\d.]+)"?', txt)
                if m:
                    try:
                        val = float(m.group(1))
                        if 1.0 <= val <= 99_999.0:
                            result["regular_price"] = val
                            break
                    except ValueError:
                        pass

        # ── Pick the best price ──
        # Priority: pre/post market price first (we're in extended session),
        # fall back to regular market price
        result["price"] = (
            result["pre_price"]
            or result["post_price"]
            or result["regular_price"]
        )

        # Save a short HTML snippet for debugging (first fin-streamer block)
        snippet_tag = soup.find("fin-streamer")
        if snippet_tag:
            result["raw_html_snippet"] = str(snippet_tag)[:200]

    except requests.exceptions.RequestException as e:
        result["error"] = f"网络错误: {e}"
    except Exception as e:
        result["error"] = f"解析错误: {e}"

    return result


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  OHLCV DATA — yfinance (prepost=True to include extended bars)
# ╚══════════════════════════════════════════════════════════════════════════════

def fetch_data(ticker, interval="5m", period="1d"):
    try:
        df = yf.download(
            ticker, interval=interval, period=period,
            auto_adjust=True, prepost=True, progress=False
        )
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.dropna(inplace=True)
        return df
    except Exception as e:
        st.error(f"yfinance 数据获取失败: {e}")
        return None


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  INDICATORS
# ╚══════════════════════════════════════════════════════════════════════════════

def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = -delta.clip(upper=0).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def compute_macd(series, fast=12, slow=26, signal=9):
    ef = series.ewm(span=fast).mean()
    es = series.ewm(span=slow).mean()
    m  = ef - es
    return m, m.ewm(span=signal).mean()

def compute_bollinger(series, period=20, std=2):
    mid = series.rolling(period).mean()
    sd  = series.rolling(period).std()
    return mid + std*sd, mid, mid - std*sd

def compute_vwap(df):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    return (tp * df["Volume"]).cumsum() / df["Volume"].cumsum()

def generate_signal(df, target_profit, shares):
    close = df["Close"]
    rsi   = compute_rsi(close)
    macd, macd_sig = compute_macd(close)
    bb_up, bb_mid, bb_lo = compute_bollinger(close)
    vwap  = compute_vwap(df)

    lr  = float(rsi.iloc[-1])
    lm  = float(macd.iloc[-1])
    lms = float(macd_sig.iloc[-1])
    lc  = float(close.iloc[-1])
    lbl = float(bb_lo.iloc[-1])
    lbu = float(bb_up.iloc[-1])
    lv  = float(vwap.iloc[-1])

    score = 0; reasons = []

    if lr < 35:   score += 2; reasons.append(f"RSI 超卖 ({lr:.1f})")
    elif lr > 65: score -= 2; reasons.append(f"RSI 超买 ({lr:.1f})")

    if lm > lms:  score += 1; reasons.append("MACD 金叉 ↑")
    else:         score -= 1; reasons.append("MACD 死叉 ↓")

    if lc < lbl:   score += 2; reasons.append("价格跌破布林下轨")
    elif lc > lbu: score -= 2; reasons.append("价格突破布林上轨")

    if lc > lv:  score += 1; reasons.append(f"价格高于 VWAP (${lv:.2f})")
    else:        score -= 1; reasons.append(f"价格低于 VWAP (${lv:.2f})")

    pm = target_profit / shares
    if score >= 3:
        action = "BUY";        entry = lc
        tp = round(entry + pm, 2);  sl = round(entry - pm*0.5, 2)
    elif score <= -3:
        action = "SELL/SHORT"; entry = lc
        tp = round(entry - pm, 2);  sl = round(entry + pm*0.5, 2)
    else:
        action = "HOLD";       entry = lc
        tp = round(entry + pm, 2);  sl = round(entry - pm*0.5, 2)

    return dict(action=action, score=score, entry=entry, take_profit=tp, stop_loss=sl,
                rsi=lr, macd=lm, macd_sig=lms,
                bb_upper=lbu, bb_lower=lbl, vwap=lv, reasons=reasons,
                rsi_series=rsi, macd_series=macd, macd_sig_series=macd_sig,
                bb_up=bb_up, bb_lo=bb_lo, bb_mid=bb_mid, vwap_series=vwap)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  CHART
# ╚══════════════════════════════════════════════════════════════════════════════

def build_chart(df, sig):
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.55, 0.25, 0.20], vertical_spacing=0.03,
                        subplot_titles=["价格 + 布林 + VWAP", "RSI (14)", "MACD"])

    fig.add_trace(go.Candlestick(x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"],
        increasing_line_color="#00e676", decreasing_line_color="#ff1744",
        name="K线"), row=1, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=sig["bb_up"],
        line=dict(color="#7c4dff", width=1, dash="dot"), name="BB上轨"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig["bb_lo"],
        line=dict(color="#7c4dff", width=1, dash="dot"),
        fill="tonexty", fillcolor="rgba(124,77,255,0.07)", name="BB下轨"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig["bb_mid"],
        line=dict(color="#7c4dff", width=0.5), name="BB中轨", showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig["vwap_series"],
        line=dict(color="#ffab00", width=1.5), name="VWAP"), row=1, col=1)

    lc = {"BUY":"#00e676","SELL/SHORT":"#ff1744","HOLD":"#ffab00"}.get(sig["action"],"#fff")
    for price, label in [(sig["entry"],"入场"),(sig["take_profit"],"止盈"),(sig["stop_loss"],"止损")]:
        fig.add_hline(y=price, line_color=lc, line_dash="dash", line_width=1,
                      annotation_text=f"{label} ${price}", annotation_position="right", row=1, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=sig["rsi_series"],
        line=dict(color="#40c4ff", width=1.5), name="RSI"), row=2, col=1)
    fig.add_hline(y=70, line_color="#ff1744", line_dash="dot", line_width=0.8, row=2, col=1)
    fig.add_hline(y=30, line_color="#00e676", line_dash="dot", line_width=0.8, row=2, col=1)

    diff = sig["macd_series"] - sig["macd_sig_series"]
    fig.add_trace(go.Bar(x=df.index, y=diff,
        marker_color=["#00e676" if v >= 0 else "#ff1744" for v in diff],
        name="MACD柱", showlegend=False), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig["macd_series"],
        line=dict(color="#00e676", width=1), name="MACD"), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig["macd_sig_series"],
        line=dict(color="#ff1744", width=1), name="Signal"), row=3, col=1)

    fig.update_layout(height=680, template="plotly_dark",
                      paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
                      showlegend=True, legend=dict(orientation="h", y=1.02, x=0),
                      xaxis_rangeslider_visible=False, font=dict(size=11))
    return fig


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TTS
# ╚══════════════════════════════════════════════════════════════════════════════

def inject_tts(text: str, lang: str = "zh-CN", rate: float = 0.95):
    safe = text.replace("'", "\\'").replace("\n", " ").replace('"', '\\"')
    uid  = abs(hash(text + str(time.time()))) % 10_000_000
    st.components.v1.html(f"""
    <div id="tts_{uid}" style="display:none;"></div>
    <script>
    (function() {{
      if (!window.speechSynthesis) return;
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance('{safe}');
      u.lang = '{lang}'; u.rate = {rate}; u.pitch = 1.0;
      function speak() {{
        var vv = window.speechSynthesis.getVoices();
        var zh = vv.find(function(v){{ return v.lang.startsWith('{lang[:2]}'); }});
        if (zh) u.voice = zh;
        window.speechSynthesis.speak(u);
      }}
      if (window.speechSynthesis.getVoices().length > 0) {{ speak(); }}
      else {{ window.speechSynthesis.onvoiceschanged = speak; }}
    }})();
    </script>
    """, height=0)

def build_speech_text(ticker, sig, shares, lang, session_label):
    a = sig["action"]; p = sig["entry"]; tp = sig["take_profit"]
    sl = sig["stop_loss"]; sc = abs(sig["score"])
    r  = "，".join(sig["reasons"][:2])
    if lang.startswith("en"):
        prefix = f"Session: {session_label}. "
        if a == "BUY":
            return (f"{prefix}BUY signal on {ticker}, strength {sc}/6. "
                    f"Price {p:.2f}. Buy {shares} shares. TP {tp:.2f}, SL {sl:.2f}. Manage risk.")
        elif a == "SELL/SHORT":
            return (f"{prefix}SELL signal on {ticker}, strength {sc}/6. "
                    f"Price {p:.2f}. Sell {shares} shares. TP {tp:.2f}, SL {sl:.2f}. Manage risk.")
        else:
            return f"{prefix}{ticker} no clear signal. Score {sig['score']}. Price {p:.2f}. Monitoring."
    else:
        prefix = f"当前{session_label}，"
        if a == "BUY":
            return (f"{prefix}交易信号！{ticker} 买入，强度 {sc} 分。"
                    f"价格 {p:.2f} 美元，买入 {shares} 股。止盈 {tp:.2f}，止损 {sl:.2f}。依据：{r}。")
        elif a == "SELL/SHORT":
            return (f"{prefix}交易信号！{ticker} 卖出，强度 {sc} 分。"
                    f"价格 {p:.2f} 美元，卖出 {shares} 股。止盈 {tp:.2f}，止损 {sl:.2f}。依据：{r}。")
        else:
            return (f"{prefix}{ticker} 无明确信号，建议观望。"
                    f"强度 {sig['score']} 分，价格 {p:.2f} 美元，持续监控中。")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SESSION STATE
# ╚══════════════════════════════════════════════════════════════════════════════

for k, v in [("last_spoken_signal", None), ("tts_enabled", True), ("tts_lang", "zh-CN"),
             ("scraper_debug", False)]:
    if k not in st.session_state:
        st.session_state[k] = v


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  PAGE HEADER
# ╚══════════════════════════════════════════════════════════════════════════════

st.title("⚡ TSLA / TSLL 日内交易助手")
st.caption("目标：每天赚 $100 | RSI + MACD + 布林带 + VWAP | 盘前/盘后/夜盘爬取 uk.finance.yahoo.com")

sess   = get_trading_session()
et_str = sess["et"].strftime("%Y-%m-%d %H:%M:%S")

badge_bg = {"PRE":"#3d1f8f","REGULAR":"#0d3b1f","POST":"#5c2a00",
            "NIGHT":"#002b4f","CLOSED":"#2a2a2a"}.get(sess["session"],"#222")

st.markdown(
    f'<span class="session-badge" style="background:{badge_bg};color:{sess["color"]};'
    f'border:1px solid {sess["color"]};">'
    f'🕐 {sess["label"]}  |  ET {et_str}  |  {sess["tz"]}</span>',
    unsafe_allow_html=True
)
st.markdown("")

if sess["session"] == "CLOSED":
    st.warning("⏸ 当前市场休市，数据仅供参考。")
elif sess["use_scraper"]:
    st.info(f"🕷 **{sess['label']}** — 爬取 `uk.finance.yahoo.com` 获取延伸时段实时报价")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SIDEBAR
# ╚══════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.header("⚙️ 交易设置")
    ticker   = st.selectbox("选择股票", ["TSLA", "TSLL"], index=0)
    interval = st.selectbox("K线周期", ["1m","2m","5m","15m","30m"], index=2)
    period   = st.selectbox("数据范围", ["1d","2d","5d"], index=0)

    st.divider()
    st.subheader("💰 目标计算")
    target  = st.number_input("今日目标利润 ($)", min_value=50, max_value=500, value=100, step=10)
    capital = st.number_input("可用资金 ($)", min_value=500, max_value=50000, value=3000, step=500)

    st.divider()
    st.subheader("🔊 语音播报")
    tts_on  = st.toggle("启用语音播报", value=st.session_state["tts_enabled"])
    st.session_state["tts_enabled"] = tts_on
    lang_choice = st.selectbox("播报语言", ["zh-CN 普通话","zh-TW 繁体中文","en-US 英文"], index=0)
    lang_map    = {"zh-CN 普通话":"zh-CN","zh-TW 繁体中文":"zh-TW","en-US 英文":"en-US"}
    active_lang = lang_map[lang_choice]
    tts_rate        = st.slider("语速", 0.5, 1.5, 0.95, 0.05)
    tts_only_action = st.checkbox("仅 BUY/SELL 时播报", value=True)

    st.divider()
    auto_refresh = st.checkbox("🔄 自动刷新 (30秒)", value=False)
    st.session_state["scraper_debug"] = st.checkbox("🐛 显示爬虫调试信息", value=False)
    if st.button("🔍 立即分析", type="primary", use_container_width=True):
        st.session_state["last_spoken_signal"] = None
        st.cache_data.clear()
        st.rerun()

    st.divider()
    st.subheader("🗓 交易时段（富途）")
    if sess["dst"]:
        st.caption("☀️ 夏令时 EDT")
        st.markdown("- **盘前** 北京 16:00–21:30\n- **盘中** 北京 21:30–04:00\n- **盘后** 北京 04:00–08:00\n- **夜盘** 北京 08:00–16:00")
    else:
        st.caption("❄️ 冬令时 EST")
        st.markdown("- **盘前** 北京 17:00–22:30\n- **盘中** 北京 22:30–05:00\n- **盘后** 北京 05:00–09:00\n- **夜盘** 北京 09:00–17:00")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  DATA FETCH
# ╚══════════════════════════════════════════════════════════════════════════════

scraped        = None
scraper_ok     = False
scraper_msg    = ""

if sess["use_scraper"]:
    with st.spinner(f"🕷 爬取 uk.finance.yahoo.com/quote/{ticker}/ …"):
        scraped = scrape_uk_yahoo(ticker)

    if scraped.get("error"):
        scraper_msg = f"❌ 爬虫错误：{scraped['error']} — 回退到 yfinance"
    elif scraped.get("price"):
        scraper_ok  = True
        # Identify which sub-field supplied the price
        src_field = (
            "preMarketPrice"  if scraped["price"] == scraped.get("pre_price")  else
            "postMarketPrice" if scraped["price"] == scraped.get("post_price") else
            "regularMarketPrice"
        )
        scraper_msg = f"✅ 爬虫成功 · 字段: `{src_field}` · 来源: uk.finance.yahoo.com"
    else:
        scraper_msg = "⚠️ 爬虫未找到价格 — 回退到 yfinance"

# Always load historical K-line bars (prepost=True includes extended-hour bars)
with st.spinner(f"📊 载入 {ticker} K线数据…"):
    df = fetch_data(ticker, interval=interval, period=period)

if df is None or len(df) < 30:
    st.error("K线数据不足（<30 根），请稍后重试或更换周期。")
    st.stop()

# Patch last Close with live scraped price when available
if scraper_ok and scraped.get("price"):
    current_price = scraped["price"]
    df.iloc[-1, df.columns.get_loc("Close")] = current_price
else:
    current_price = float(df["Close"].iloc[-1])

shares_estimate = max(1, int(capital / current_price))
sig = generate_signal(df, target, shares_estimate)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TTS TRIGGER
# ╚══════════════════════════════════════════════════════════════════════════════

signal_key   = f"{ticker}|{sig['action']}|{sig['entry']:.2f}"
skip_hold    = tts_only_action and sig["action"] == "HOLD"
should_speak = tts_on and not skip_hold and signal_key != st.session_state["last_spoken_signal"]

if should_speak:
    txt = build_speech_text(ticker, sig, shares_estimate, active_lang, sess["label"])
    inject_tts(txt, lang=active_lang, rate=tts_rate)
    st.session_state["last_spoken_signal"] = signal_key


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SCRAPER STATUS
# ╚══════════════════════════════════════════════════════════════════════════════

if sess["use_scraper"]:
    cols = st.columns([3, 1, 1, 1])
    with cols[0]:
        if scraper_ok:   st.success(scraper_msg)
        elif "❌" in scraper_msg: st.error(scraper_msg)
        else:            st.warning(scraper_msg)

    if scraper_ok and scraped:
        with cols[1]:
            st.metric("正式收盘", f"${scraped['regular_price']:.2f}" if scraped.get("regular_price") else "—")
        with cols[2]:
            st.metric("盘前报价", f"${scraped['pre_price']:.2f}"     if scraped.get("pre_price")     else "—")
        with cols[3]:
            st.metric("盘后报价", f"${scraped['post_price']:.2f}"    if scraped.get("post_price")    else "—")

    # Debug panel (optional)
    if st.session_state["scraper_debug"] and scraped:
        with st.expander("🐛 爬虫调试信息"):
            st.json({k: v for k, v in scraped.items() if k != "raw_html_snippet"})
            if scraped.get("raw_html_snippet"):
                st.code(scraped["raw_html_snippet"], language="html")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  METRICS ROW
# ╚══════════════════════════════════════════════════════════════════════════════

st.divider()
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("当前价格",  f"${current_price:.2f}",
          delta="爬虫实时" if scraper_ok else "yfinance")
c2.metric("RSI",       f"{sig['rsi']:.1f}",
          delta="超卖" if sig["rsi"]<35 else ("超买" if sig["rsi"]>65 else "中性"))
c3.metric("VWAP",      f"${sig['vwap']:.2f}")
c4.metric("可买股数",  f"{shares_estimate} 股", delta=f"资金 ${capital:,}")
c5.metric("目标利润",  f"${target}",
          delta=f"每股需涨 ${target/shares_estimate:.2f}")

st.divider()

# ── TTS bar ───────────────────────────────────────────────────────────────────
icon_map = {"BUY":"🟢","SELL/SHORT":"🔴","HOLD":"🟡"}
bar_l, bar_r = st.columns([4, 1])
with bar_l:
    if not tts_on:
        st.warning("🔕 语音播报已关闭")
    elif should_speak:
        st.success(f"🔊 正在播报：{icon_map.get(sig['action'],'?')} **{sig['action']}** — {ticker} @ ${sig['entry']:.2f}（{sess['label']}）")
    elif skip_hold:
        st.info("🔇 HOLD 信号，已跳过播报")
    else:
        st.info(f"🔇 等待新信号（当前：{icon_map.get(sig['action'],'?')} {sig['action']}）")
with bar_r:
    if tts_on and st.button("🔊 重新播报", use_container_width=True):
        txt = build_speech_text(ticker, sig, shares_estimate, active_lang, sess["label"])
        inject_tts(txt, lang=active_lang, rate=tts_rate)

st.divider()


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  SIGNAL CARD + CHART
# ╚══════════════════════════════════════════════════════════════════════════════

col_sig, col_detail = st.columns([1, 2])

with col_sig:
    css = {"BUY":"signal-buy","SELL/SHORT":"signal-sell","HOLD":"signal-hold"}.get(sig["action"],"signal-hold")
    st.markdown(f"""
    <div style="background:#161b22;border-radius:12px;padding:24px;text-align:center;border:1px solid #2d3748;">
      <div style="font-size:1rem;color:#8b949e;margin-bottom:8px;">信号强度: {sig['score']:+d}/6</div>
      <div class="{css}">{icon_map.get(sig['action'],'')} {sig['action']}</div>
      <div style="margin-top:16px;font-size:0.9rem;color:#8b949e;">
        {'&nbsp;'.join(['●']*abs(sig['score']) + ['○']*(6-abs(sig['score'])))}
      </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown("### 📋 交易计划")
    pc = "#00e676" if sig["action"]=="BUY" else ("#ff1744" if sig["action"]=="SELL/SHORT" else "#ffab00")
    st.markdown(f"""
    <div style="background:#161b22;border-radius:10px;padding:16px;border-left:4px solid {pc};">
      <p>🎯 <b>入场价</b>: ${sig['entry']:.2f}</p>
      <p>✅ <b>止盈价</b>: ${sig['take_profit']:.2f}</p>
      <p>🛑 <b>止损价</b>: ${sig['stop_loss']:.2f}</p>
      <p>📦 <b>股  数</b>: {shares_estimate} 股</p>
      <p>💵 <b>预期盈利</b>: ${(sig['take_profit']-sig['entry'])*shares_estimate:.2f}</p>
      <p>⚠️ <b>最大亏损</b>: ${abs((sig['stop_loss']-sig['entry'])*shares_estimate):.2f}</p>
      <p>📊 <b>盈亏比</b>: {abs((sig['take_profit']-sig['entry'])/(sig['stop_loss']-sig['entry'])):.1f}x</p>
    </div>
    """, unsafe_allow_html=True)

with col_detail:
    st.markdown("### 📊 信号依据")
    for r in sig["reasons"]:
        ok = any(x in r for x in ["超卖","金叉","下轨","高于"])
        st.markdown(f"{'✅' if ok else '⚠️'} {r}")

    st.markdown("### 📈 K线图表")
    if sess["use_scraper"]:
        st.caption("⚠️ 延伸时段流动性较低，K线仅供参考 · 最新收盘已替换为爬虫实时价")
    st.plotly_chart(build_chart(df, sig), use_container_width=True)


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  TIPS
# ╚══════════════════════════════════════════════════════════════════════════════

st.divider()
st.markdown("### 💡 日内交易策略提示")
t1, t2, t3 = st.columns(3)
with t1:
    st.info("**TSLA vs TSLL**\n\nTSLL 是 TSLA 的 2x 杠杆 ETF，延伸时段流动性请先在券商确认再操作。")
with t2:
    st.warning("**延伸时段注意**\n\n盘前/盘后/夜盘价差大、流动性低，止损建议设更宽，仓位缩小 50%。")
with t3:
    st.error("**风险提示**\n\n此工具仅供参考，不构成投资建议。延伸时段风险更高，请谨慎操作。")


# ╔══════════════════════════════════════════════════════════════════════════════
# ║  AUTO-REFRESH + FOOTER
# ╚══════════════════════════════════════════════════════════════════════════════

if auto_refresh:
    time.sleep(30)
    st.rerun()

st.caption(
    f"最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
    f"ET: {et_str} | 时段: {sess['label']} | "
    f"价格来源: {'uk.finance.yahoo.com (爬虫)' if scraper_ok else 'yfinance'}"
)
