import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime
import time

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
  .stAlert { border-radius: 10px; }
</style>
""", unsafe_allow_html=True)

# ── Web Speech TTS ────────────────────────────────────────────────────────────
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
      u.lang  = '{lang}';
      u.rate  = {rate};
      u.pitch = 1.0;
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

def build_speech_text(ticker, sig, shares, lang):
    action = sig['action']
    price  = sig['entry']
    tp     = sig['take_profit']
    sl     = sig['stop_loss']
    score  = abs(sig['score'])
    reason = '，'.join(sig['reasons'][:2])

    if lang.startswith("en"):
        if action == "BUY":
            return (f"Trading signal! {ticker} BUY signal, strength {score} out of 6. "
                    f"Current price {price:.2f} dollars. Buy {shares} shares. "
                    f"Take profit at {tp:.2f}, stop loss at {sl:.2f}. "
                    f"Please manage your risk carefully.")
        elif action == "SELL/SHORT":
            return (f"Trading signal! {ticker} SELL signal, strength {score} out of 6. "
                    f"Current price {price:.2f} dollars. Sell {shares} shares. "
                    f"Take profit at {tp:.2f}, stop loss at {sl:.2f}. "
                    f"Please manage your risk carefully.")
        else:
            return (f"{ticker} no clear signal. Score {sig['score']}. "
                    f"Current price {price:.2f} dollars. Continue monitoring.")
    else:
        if action == "BUY":
            return (f"交易信号！{ticker} 买入信号，信号强度 {score} 分。"
                    f"当前价格 {price:.2f} 美元，建议买入 {shares} 股。"
                    f"止盈目标 {tp:.2f} 美元，止损价位 {sl:.2f} 美元。"
                    f"依据：{reason}。请注意风险，严格执行止损。")
        elif action == "SELL/SHORT":
            return (f"交易信号！{ticker} 卖出信号，信号强度 {score} 分。"
                    f"当前价格 {price:.2f} 美元，建议卖出 {shares} 股。"
                    f"止盈目标 {tp:.2f} 美元，止损价位 {sl:.2f} 美元。"
                    f"依据：{reason}。请注意风险，严格执行止损。")
        else:
            return (f"{ticker} 当前无明确信号，建议观望。"
                    f"信号强度 {sig['score']} 分，尚未达到交易门槛。"
                    f"当前价格 {price:.2f} 美元，继续监控中。")

# ── Indicators ────────────────────────────────────────────────────────────────
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
    s  = m.ewm(span=signal).mean()
    return m, s

def compute_bollinger(series, period=20, std=2):
    mid = series.rolling(period).mean()
    sd  = series.rolling(period).std()
    return mid + std*sd, mid, mid - std*sd

def compute_vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    return (tp * df['Volume']).cumsum() / df['Volume'].cumsum()

def fetch_data(ticker, interval='5m', period='1d'):
    try:
        df = yf.download(ticker, interval=interval, period=period,
                         auto_adjust=True, progress=False)
        if df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.dropna(inplace=True)
        return df
    except Exception as e:
        st.error(f"数据获取失败: {e}")
        return None

def generate_signal(df, target_profit, shares):
    close = df['Close']
    rsi   = compute_rsi(close)
    macd, macd_sig = compute_macd(close)
    bb_up, bb_mid, bb_lo = compute_bollinger(close)
    vwap  = compute_vwap(df)

    lr    = float(rsi.iloc[-1])
    lm    = float(macd.iloc[-1])
    lms   = float(macd_sig.iloc[-1])
    lc    = float(close.iloc[-1])
    lbbl  = float(bb_lo.iloc[-1])
    lbbu  = float(bb_up.iloc[-1])
    lv    = float(vwap.iloc[-1])

    score   = 0
    reasons = []

    if lr < 35:
        score += 2; reasons.append(f"RSI 超卖 ({lr:.1f})")
    elif lr > 65:
        score -= 2; reasons.append(f"RSI 超买 ({lr:.1f})")

    if lm > lms:
        score += 1; reasons.append("MACD 金叉 ↑")
    else:
        score -= 1; reasons.append("MACD 死叉 ↓")

    if lc < lbbl:
        score += 2; reasons.append("价格跌破布林下轨")
    elif lc > lbbu:
        score -= 2; reasons.append("价格突破布林上轨")

    if lc > lv:
        score += 1; reasons.append(f"价格高于 VWAP (${lv:.2f})")
    else:
        score -= 1; reasons.append(f"价格低于 VWAP (${lv:.2f})")

    pm = target_profit / shares
    if score >= 3:
        action = "BUY";        entry = lc
        tp = round(entry + pm, 2);      sl = round(entry - pm * 0.5, 2)
    elif score <= -3:
        action = "SELL/SHORT"; entry = lc
        tp = round(entry - pm, 2);      sl = round(entry + pm * 0.5, 2)
    else:
        action = "HOLD";       entry = lc
        tp = round(entry + pm, 2);      sl = round(entry - pm * 0.5, 2)

    return dict(action=action, score=score, entry=entry, take_profit=tp, stop_loss=sl,
                rsi=lr, macd=lm, macd_sig=lms,
                bb_upper=lbbu, bb_lower=lbbl, vwap=lv, reasons=reasons,
                rsi_series=rsi, macd_series=macd, macd_sig_series=macd_sig,
                bb_up=bb_up, bb_lo=bb_lo, bb_mid=bb_mid, vwap_series=vwap)

def build_chart(df, sig):
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True,
                        row_heights=[0.55, 0.25, 0.20], vertical_spacing=0.03,
                        subplot_titles=["价格 + 布林 + VWAP", "RSI (14)", "MACD"])

    fig.add_trace(go.Candlestick(x=df.index, open=df['Open'], high=df['High'],
        low=df['Low'], close=df['Close'],
        increasing_line_color='#00e676', decreasing_line_color='#ff1744', name="K线"), row=1, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=sig['bb_up'], line=dict(color='#7c4dff', width=1, dash='dot'), name="BB上轨"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig['bb_lo'], line=dict(color='#7c4dff', width=1, dash='dot'), fill='tonexty', fillcolor='rgba(124,77,255,0.07)', name="BB下轨"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig['bb_mid'], line=dict(color='#7c4dff', width=0.5), name="BB中轨", showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig['vwap_series'], line=dict(color='#ffab00', width=1.5), name="VWAP"), row=1, col=1)

    lc = {"BUY":"#00e676","SELL/SHORT":"#ff1744","HOLD":"#ffab00"}.get(sig['action'],"#fff")
    for price, label in [(sig['entry'],"入场"),(sig['take_profit'],"止盈"),(sig['stop_loss'],"止损")]:
        fig.add_hline(y=price, line_color=lc, line_dash="dash", line_width=1,
                      annotation_text=f"{label} ${price}", annotation_position="right", row=1, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=sig['rsi_series'], line=dict(color='#40c4ff', width=1.5), name="RSI"), row=2, col=1)
    fig.add_hline(y=70, line_color='#ff1744', line_dash='dot', line_width=0.8, row=2, col=1)
    fig.add_hline(y=30, line_color='#00e676', line_dash='dot', line_width=0.8, row=2, col=1)

    diff = sig['macd_series'] - sig['macd_sig_series']
    fig.add_trace(go.Bar(x=df.index, y=diff, marker_color=['#00e676' if v>=0 else '#ff1744' for v in diff], name="MACD柱", showlegend=False), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig['macd_series'], line=dict(color='#00e676', width=1), name="MACD"), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=sig['macd_sig_series'], line=dict(color='#ff1744', width=1), name="Signal"), row=3, col=1)

    fig.update_layout(height=680, template="plotly_dark",
                      paper_bgcolor="#0e1117", plot_bgcolor="#161b22",
                      showlegend=True, legend=dict(orientation="h", y=1.02, x=0),
                      xaxis_rangeslider_visible=False, font=dict(size=11))
    return fig

# ── Session state ─────────────────────────────────────────────────────────────
for k, v in [('last_spoken_signal', None), ('tts_enabled', True), ('tts_lang', 'zh-CN')]:
    if k not in st.session_state:
        st.session_state[k] = v

# ── Title ─────────────────────────────────────────────────────────────────────
st.title("⚡ TSLA / TSLL 日内交易助手")
st.caption("目标：每天赚 $100 | 基于 RSI + MACD + 布林带 + VWAP")

# ── Sidebar ───────────────────────────────────────────────────────────────────
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
    tts_on   = st.toggle("启用语音播报", value=st.session_state['tts_enabled'])
    st.session_state['tts_enabled'] = tts_on

    lang_choice = st.selectbox("播报语言", ["zh-CN 普通话", "zh-TW 繁体中文", "en-US 英文"], index=0)
    lang_map     = {"zh-CN 普通话": "zh-CN", "zh-TW 繁体中文": "zh-TW", "en-US 英文": "en-US"}
    active_lang  = lang_map[lang_choice]

    tts_rate        = st.slider("语速", 0.5, 1.5, 0.95, 0.05)
    tts_only_action = st.checkbox("仅 BUY/SELL 时播报（跳过 HOLD）", value=True)

    st.divider()
    auto_refresh = st.checkbox("🔄 自动刷新 (30秒)", value=False)
    if st.button("🔍 立即分析", type="primary", use_container_width=True):
        st.session_state['last_spoken_signal'] = None
        st.rerun()

# ── Data & signal ─────────────────────────────────────────────────────────────
with st.spinner(f"正在获取 {ticker} 数据..."):
    df = fetch_data(ticker, interval=interval, period=period)

if df is None or len(df) < 30:
    st.error("数据不足，请稍后重试或更换周期。")
    st.stop()

current_price   = float(df['Close'].iloc[-1])
shares_estimate = max(1, int(capital / current_price))
sig = generate_signal(df, target, shares_estimate)

# ── TTS logic ─────────────────────────────────────────────────────────────────
# Key includes price rounded to 2dp so it re-speaks on meaningful price changes
signal_key  = f"{ticker}|{sig['action']}|{sig['entry']:.2f}"
skip_hold   = tts_only_action and sig['action'] == "HOLD"
should_speak = tts_on and not skip_hold and signal_key != st.session_state['last_spoken_signal']

if should_speak:
    txt = build_speech_text(ticker, sig, shares_estimate, active_lang)
    inject_tts(txt, lang=active_lang, rate=tts_rate)
    st.session_state['last_spoken_signal'] = signal_key

# ── Metrics row ───────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("当前价格",  f"${current_price:.2f}")
c2.metric("RSI",       f"{sig['rsi']:.1f}", delta="超卖" if sig['rsi']<35 else ("超买" if sig['rsi']>65 else "中性"))
c3.metric("VWAP",      f"${sig['vwap']:.2f}")
c4.metric("可买股数",  f"{shares_estimate} 股", delta=f"资金 ${capital:,}")
c5.metric("目标利润",  f"${target}",            delta=f"每股需涨 ${target/shares_estimate:.2f}")

st.divider()

# ── TTS status bar ────────────────────────────────────────────────────────────
icon_map = {"BUY":"🟢","SELL/SHORT":"🔴","HOLD":"🟡"}
bar_l, bar_r = st.columns([4, 1])
with bar_l:
    if not tts_on:
        st.warning("🔕 语音播报已关闭，可在左侧栏开启")
    elif should_speak:
        st.success(f"🔊 正在播报：{icon_map.get(sig['action'],'?')} **{sig['action']}** — {ticker} @ ${sig['entry']:.2f}")
    elif skip_hold:
        st.info(f"🔇 HOLD 信号，已跳过播报（可在侧栏关闭此选项）")
    else:
        st.info(f"🔇 等待新信号（当前：{icon_map.get(sig['action'],'?')} {sig['action']} — 与上次相同）")
with bar_r:
    if tts_on and st.button("🔊 重新播报", use_container_width=True):
        txt = build_speech_text(ticker, sig, shares_estimate, active_lang)
        inject_tts(txt, lang=active_lang, rate=tts_rate)

st.divider()

# ── Signal card + chart ───────────────────────────────────────────────────────
col_sig, col_detail = st.columns([1, 2])

with col_sig:
    css = {"BUY":"signal-buy","SELL/SHORT":"signal-sell","HOLD":"signal-hold"}.get(sig['action'],"signal-hold")
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
    pc = "#00e676" if sig['action']=="BUY" else ("#ff1744" if sig['action']=="SELL/SHORT" else "#ffab00")
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
    for r in sig['reasons']:
        ok = any(x in r for x in ["超卖","金叉","下轨","高于"])
        st.markdown(f"{'✅' if ok else '⚠️'} {r}")

    st.markdown("### 📈 实时图表")
    st.plotly_chart(build_chart(df, sig), use_container_width=True)

# ── Tips ──────────────────────────────────────────────────────────────────────
st.divider()
st.markdown("### 💡 日内交易策略提示")
t1, t2, t3 = st.columns(3)
with t1:
    st.info("**TSLA vs TSLL**\n\nTSLL 是 TSLA 的 2x 杠杆 ETF，波动更大，同样资金下更容易达到 $100 目标，但风险也翻倍。")
with t2:
    st.warning("**最佳交易时段**\n\n美东时间 9:30–11:00 和 14:30–16:00 是波动最大的时段，信号更可靠。")
with t3:
    st.error("**风险提示**\n\n此工具仅供参考，不构成投资建议。日内交易有亏损风险，请严格执行止损策略。")

# ── Auto-refresh ──────────────────────────────────────────────────────────────
if auto_refresh:
    time.sleep(30)
    st.rerun()

st.caption(f"最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据来源: Yahoo Finance")
