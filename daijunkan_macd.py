#!/usr/bin/env python3
"""Gate.io 先物 × 大循環MACD シグナル判定＆複利自動トレードシステム。

機能:
1. マルチタイムフレーム（15m / 1h / 2h / 4h / 1d）の大循環ステージ（第1〜第6）と
   大循環MACD（MACD1: 短-中, MACD2: 短-長, MACD3: 中-長）のリアルタイム計算
2. マルチ銘柄スキャン（BTC 200倍, ETH 200倍, SOL 100倍 ＋ 平日 XAU/NVDA/NAS100 100倍）
3. 複利ポジションサイズ自動計算（口座残高の増加に比例して自動でロットを引き上げ）
4. Gate.io API v4 完全連携による自動エントリー・自動利確(TP)・自動損切り(SL)・トレーリングストップ
"""

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 銘柄ごとの最大レバレッジと1コントラクトあたりの通貨単位（quanto_multiplier）
CONTRACT_SPECS = {
    "BTC_USDT": {"max_lev": 200, "multiplier": 0.0001, "price_Step": 0.1},
    "ETH_USDT": {"max_lev": 200, "multiplier": 0.01, "price_Step": 0.01},
    "SOL_USDT": {"max_lev": 100, "multiplier": 1.0, "price_Step": 0.01},
    "XRP_USDT": {"max_lev": 100, "multiplier": 10.0, "price_Step": 0.0001},
    "XAU_USDT": {"max_lev": 100, "multiplier": 0.001, "price_Step": 0.1},
    "NVDA_USDT": {"max_lev": 100, "multiplier": 0.01, "price_Step": 0.01},
    "NAS100_USDT": {"max_lev": 100, "multiplier": 0.001, "price_Step": 0.1},
}


def load_dotenv(env_path: str = "/Users/apo/Development/.env") -> None:
    """ローカルの .env ファイルから環境変数を読み込む。"""
    path = Path(env_path)
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


@dataclass
class TimeframeAnalysis:
    """各時間足の大循環MACD分析結果を保持するデータクラス。"""

    interval: str
    close: float
    high: float
    low: float
    ema5: float
    ema20: float
    ema40: float
    stage: int
    prev_stage: int
    stage_name: str
    macd1: float
    prev_macd1: float
    macd2: float
    prev_macd2: float
    macd3: float
    prev_macd3: float
    atr: float

    @property
    def dir1(self) -> int:
        """MACD1の傾き（1: 上向き, -1: 下向き）。"""
        return 1 if self.macd1 >= self.prev_macd1 else -1

    @property
    def dir2(self) -> int:
        """MACD2の傾き（1: 上向き, -1: 下向き）。"""
        return 1 if self.macd2 >= self.prev_macd2 else -1

    @property
    def dir3(self) -> int:
        """MACD3の傾き（1: 上向き, -1: 下向き）。"""
        return 1 if self.macd3 >= self.prev_macd3 else -1


def calculate_ema(prices: list[float], period: int) -> list[float]:
    """指定期間の指数平滑移動平均（EMA）を計算する。"""
    multiplier = 2.0 / (period + 1)
    ema_values = [prices[0]]
    for price in prices[1:]:
        ema_values.append(price * multiplier + ema_values[-1] * (1.0 - multiplier))
    return ema_values


def calculate_atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """直近のATR（平均真の値幅）を計算する。"""
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / max(len(trs), 1)
    return sum(trs[-period:]) / period


def determine_stage(short_ema: float, medium_ema: float, long_ema: float) -> tuple[int, str]:
    """短期・中期・長期EMAの並び順から大循環の6ステージを判定する。"""
    if short_ema >= medium_ema >= long_ema:
        return 1, "第1ステージ（安定上昇）"
    if medium_ema >= short_ema >= long_ema:
        return 2, "第2ステージ（上昇終焉）"
    if medium_ema >= long_ema >= short_ema:
        return 3, "第3ステージ（下降入口）"
    if long_ema >= medium_ema >= short_ema:
        return 4, "第4ステージ（安定下降）"
    if long_ema >= short_ema >= medium_ema:
        return 5, "第5ステージ（下降終焉）"
    if short_ema >= long_ema >= medium_ema:
        return 6, "第6ステージ（上昇入口）"
    return 0, "不明"


def fetch_timeframe_analysis(contract: str, interval: str, limit: int = 150) -> TimeframeAnalysis:
    """Gate.io APIからローソク足を取得し、指定時間足の大循環MACDを計算する。"""
    url = (
        f"https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
        f"?contract={contract}&interval={interval}&limit={limit}"
    )
    req = urllib.request.urlopen(url, timeout=10)
    candles = json.loads(req.read().decode("utf-8"))

    closes = [float(c["c"]) for c in candles]
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]

    ema5 = calculate_ema(closes, 5)
    ema20 = calculate_ema(closes, 20)
    ema40 = calculate_ema(closes, 40)

    macd1 = [s - m for s, m in zip(ema5, ema20)]
    macd2 = [s - l for s, l in zip(ema5, ema40)]
    macd3 = [m - l for m, l in zip(ema20, ema40)]

    stage, stage_name = determine_stage(ema5[-1], ema20[-1], ema40[-1])
    prev_stage, _ = determine_stage(ema5[-2], ema20[-2], ema40[-2])
    atr = calculate_atr(highs, lows, closes, 14)

    return TimeframeAnalysis(
        interval=interval,
        close=closes[-1],
        high=highs[-1],
        low=lows[-1],
        ema5=ema5[-1],
        ema20=ema20[-1],
        ema40=ema40[-1],
        stage=stage,
        prev_stage=prev_stage,
        stage_name=stage_name,
        macd1=macd1[-1],
        prev_macd1=macd1[-2],
        macd2=macd2[-1],
        prev_macd2=macd2[-2],
        macd3=macd3[-1],
        prev_macd3=macd3[-2],
        atr=atr,
    )


def evaluate_master_signal(analyses: dict[str, TimeframeAnalysis], decimals: int = 1) -> dict:
    """マルチタイムフレームの大循環MACDから総合売買シグナルと戦略ラインを算出する。"""
    tf_15m = analyses["15m"]
    tf_1h = analyses["1h"]
    tf_2h = analyses["2h"]
    tf_4h = analyses["4h"]
    price = tf_15m.close

    higher_bearish = tf_4h.stage == 4 and tf_2h.stage in (3, 4, 5)
    higher_bullish = tf_4h.stage == 1 and tf_2h.stage in (6, 1, 2)

    signal_type = "WAIT"
    signal_grade = "様子見・待機"
    action_summary = ""
    entry_price = price
    stop_loss = 0.0
    tp1 = 0.0
    tp2 = 0.0

    if higher_bearish:
        resistance_band_low = min(tf_1h.ema40, tf_2h.ema20)
        resistance_band_high = max(tf_1h.ema40, tf_2h.ema20)

        if tf_1h.stage == 4 and (tf_1h.dir1 == -1 or tf_15m.dir1 == -1):
            signal_type = "SHORT_NOW"
            signal_grade = "特Aシグナル：戻り売り本命（第4ステージ揃い）"
            action_summary = (
                "上位足（日足・4h・2h）と1時間足がすべて第4ステージに揃い、短期MACDも下向きです。"
                "最も勝率が高い順張りショートのタイミングです。"
            )
            entry_price = round(max(price, tf_15m.ema5), decimals)
            stop_loss = round(max(tf_1h.ema40, resistance_band_high) + tf_1h.atr * 0.35, decimals)
            tp1 = round(price - tf_4h.atr * 1.3, decimals)
            tp2 = round(price - tf_4h.atr * 2.8, decimals)
        elif (tf_1h.stage == 5 and tf_1h.dir1 == -1) or (tf_1h.stage == 4 and tf_15m.dir1 == -1):
            signal_type = "SHORT_LIMIT_WAIT"
            signal_grade = "Bシグナル：戻り売り待ち伏せ（1時間足の戻しが失速開始）"
            action_summary = (
                f"上位足（日足・4h・2h）は第4ステージ（下降）で、1時間足の戻しもMACD1が下向きに失速しました。"
                f"1時間足の長期EMA({tf_1h.ema40:.{decimals}f})と2時間足の中期EMA({tf_2h.ema20:.{decimals}f})が重なる"
                f" {resistance_band_low:.{decimals}f}〜{resistance_band_high:.{decimals}f} 付近が絶好の戻り売り天井になります。"
            )
            entry_price = round(resistance_band_low, decimals)
            stop_loss = round(resistance_band_high + tf_1h.atr * 0.5, decimals)
            tp1 = round(price - tf_4h.atr * 1.3, decimals)
            tp2 = round(price - tf_4h.atr * 2.8, decimals)
        else:
            signal_type = "WAIT"
            signal_grade = f"様子見・待機（1時間足が{tf_1h.stage_name}で反発継続中）"
            action_summary = (
                "上位足は第4ステージですが、1時間足のMACD1がまだ上向き（または第6・第1ステージ）のため、"
                "反発が終わって1時間足のMACD1が下向きに折れ曲がるまで新規ショートは待機します。"
            )
            entry_price = round(resistance_band_high, decimals)
            stop_loss = round(resistance_band_high + tf_1h.atr * 0.6, decimals)
            tp1 = round(price - tf_4h.atr * 1.2, decimals)
            tp2 = round(price - tf_4h.atr * 2.5, decimals)

    elif higher_bullish:
        support_band_high = max(tf_1h.ema40, tf_2h.ema20)
        support_band_low = min(tf_1h.ema40, tf_2h.ema20)

        if tf_1h.stage == 1 and (tf_1h.dir1 == 1 or tf_15m.dir1 == 1):
            signal_type = "LONG_NOW"
            signal_grade = "特Aシグナル：押し目買い本命（第1ステージ揃い）"
            action_summary = "上位足と1時間足が第1ステージで上向きに揃いました。順張りロングの好機です。"
            entry_price = round(min(price, tf_15m.ema5), decimals)
            stop_loss = round(min(tf_1h.ema40, support_band_low) - tf_1h.atr * 0.35, decimals)
            tp1 = round(price + tf_4h.atr * 1.3, decimals)
            tp2 = round(price + tf_4h.atr * 2.8, decimals)
        else:
            signal_type = "LONG_LIMIT_WAIT"
            signal_grade = "Bシグナル：押し目買い待ち伏せ（1時間足が押し目中）"
            action_summary = (
                f"上位足は第1ステージ（上昇）ですが、1時間足が押し目を作っています。"
                f"{support_band_low:.{decimals}f}〜{support_band_high:.{decimals}f} の帯での反発ロングを狙います。"
            )
            entry_price = round(support_band_high, decimals)
            stop_loss = round(support_band_low - tf_1h.atr * 0.5, decimals)
            tp1 = round(price + tf_4h.atr * 1.3, decimals)
            tp2 = round(price + tf_4h.atr * 2.8, decimals)

    else:
        signal_grade = f"転換期シグナル：4時間足が{tf_4h.stage_name}"
        action_summary = "上位足がステージ移行中のため、1時間足と2時間足のMACD1・2の向きが揃う方向に絞ります。"
        entry_price = round(price, decimals)
        stop_loss = round(price + tf_1h.atr if tf_1h.dir1 == -1 else price - tf_1h.atr, decimals)
        tp1 = round(price - tf_1h.atr * 1.5 if tf_1h.dir1 == -1 else price + tf_1h.atr * 1.5, decimals)
        tp2 = round(price - tf_1h.atr * 3.0 if tf_1h.dir1 == -1 else price + tf_1h.atr * 3.0, decimals)

    return {
        "signal_type": signal_type,
        "signal_grade": signal_grade,
        "action_summary": action_summary,
        "current_price": price,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "tp1": tp1,
        "tp2": tp2,
    }


class GateTrader:
    """Gate.io Futures API v4 を使って残高確認・指値発注・TP/SL設定を行うクラス。"""

    def __init__(self, api_key: str, api_secret: str) -> None:
        """APIキーとシークレットを初期化する。"""
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://api.gateio.ws"

    def _request(self, method: str, path: str, query_string: str = "", payload: dict | None = None) -> dict | list:
        """署名付きHTTPリクエストを送信してJSONレスポンスを返す。"""
        body = json.dumps(payload) if payload is not None else ""
        timestamp = str(int(time.time()))
        hashed_body = hashlib.sha512(body.encode("utf-8")).hexdigest()
        sign_string = f"{method}\n{path}\n{query_string}\n{hashed_body}\n{timestamp}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            sign_string.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "KEY": self.api_key,
            "Timestamp": timestamp,
            "SIGN": signature,
        }
        url = self.base_url + path + (f"?{query_string}" if query_string else "")
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            error_body = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Gate.io API応答 ({err.code}): {error_body}") from err

    def get_account(self) -> dict:
        """USDT先物口座の残高と証拠金情報を取得する。"""
        return self._request("GET", "/api/v4/futures/usdt/accounts")

    def get_positions(self) -> list[dict]:
        """現在保有中の全先物ポジションを取得する。"""
        positions = self._request("GET", "/api/v4/futures/usdt/positions")
        return [p for p in positions if int(p.get("size", 0)) != 0]

    def get_open_orders(self, contract: str = "BTC_USDT") -> list[dict]:
        """現在待機中の通常指値注文一覧を取得する。"""
        return self._request("GET", "/api/v4/futures/usdt/orders", f"contract={contract}&status=open")

    def get_price_orders(self, contract: str = "BTC_USDT") -> list[dict]:
        """現在待機中のトリガー注文（TP/SL逆指値）一覧を取得する。"""
        return self._request("GET", "/api/v4/futures/usdt/price_orders", f"contract={contract}&status=open")

    def set_max_leverage(self, contract: str) -> int:
        """指定銘柄のレバレッジを分離マージンの最高倍率に設定する。"""
        spec = CONTRACT_SPECS.get(contract, {"max_lev": 100})
        max_lev = spec["max_lev"]
        self._request("POST", f"/api/v4/futures/usdt/positions/{contract}/leverage", f"leverage={max_lev}")
        return max_lev

    def calculate_compounding_size(self, contract: str, price: float, equity_multiplier: float = 32.0) -> int:
        """口座の総資産（Total Equity）に応じて複利で注文コントラクト数を計算する。"""
        acc = self.get_account()
        total_equity = float(acc.get("total", 100.0))
        spec = CONTRACT_SPECS.get(contract, {"multiplier": 0.0001})
        notional_target = total_equity * equity_multiplier
        coin_amount = notional_target / price
        contracts = int(round(coin_amount / spec["multiplier"]))
        return max(contracts, 1)

    def place_limit_order(
        self,
        contract: str,
        size: int,
        price: float,
        reduce_only: bool = False,
        tif: str = "gtc",
    ) -> dict:
        """先物の指値注文を発注する（sizeが正ならロング、負ならショート）。"""
        payload = {
            "contract": contract,
            "size": size,
            "price": str(price),
            "tif": tif,
            "reduce_only": reduce_only,
        }
        return self._request("POST", "/api/v4/futures/usdt/orders", payload=payload)

    def place_price_trigger_order(
        self,
        contract: str,
        trigger_price: float,
        rule: int,
        order_type: str,
    ) -> dict:
        """ポジション決済用のトリガー注文（SLまたはTP）を発注する。

        rule: 1 = 価格がtrigger_price以上になったとき発動, 2 = 価格がtrigger_price以下になったとき発動
        order_type: 'close-short-position' または 'close-long-position'
        """
        payload = {
            "initial": {
                "contract": contract,
                "size": 0,
                "price": "0",
                "tif": "ioc",
                "reduce_only": True,
                "auto_size": "close_short" if "short" in order_type else "close_long",
            },
            "trigger": {
                "strategy_type": 0,
                "price_type": 0,
                "price": str(trigger_price),
                "rule": rule,
                "expiration": 86400 * 7,
            },
            "order_type": order_type,
        }
        return self._request("POST", "/api/v4/futures/usdt/price_orders", payload=payload)


def send_mac_notification(title: str, message: str) -> None:
    """macOSのデスクトップ通知を送信する。"""
    script = f'display notification "{message}" with title "{title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], check=False)


def print_report(contract: str, analyses: dict[str, TimeframeAnalysis], signal: dict) -> None:
    """大循環MACDの分析結果と売買シグナルを整形して表示する。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 68)
    print(f"【大循環MACD シグナル参謀レポート】 対象: {contract} ({now_str})")
    print(f"現在価格: {signal['current_price']} USDT")
    print("=" * 68)

    for tf in ["1d", "4h", "2h", "1h", "15m"]:
        a = analyses[tf]
        d1 = "↑上向き" if a.dir1 > 0 else "↓下向き"
        d2 = "↑上向き" if a.dir2 > 0 else "↓下向き"
        d3 = "↑上向き" if a.dir3 > 0 else "↓下向き"
        stage_change = f"(前回:第{a.prev_stage})" if a.stage == a.prev_stage else f"★第{a.prev_stage}から移行!"
        print(f"■ {tf:>3}足 | {a.stage_name} {stage_change}")
        print(f"        | EMA5={a.ema5:.2f} / EMA20={a.ema20:.2f} / EMA40={a.ema40:.2f}")
        print(f"        | MACD1(短-中): {a.macd1:+7.2f} [{d1}] | MACD2(短-長): {a.macd2:+7.2f} [{d2}] | MACD3(帯): {a.macd3:+7.2f} [{d3}]")

    print("-" * 68)
    print(f"◆ 総合判定: {signal['signal_grade']}")
    print(f"◆ 参謀コメント: {signal['action_summary']}")
    print("-" * 68)
    print("【大循環MACDが算出する推奨ライン】")
    print(f"  ・狙い目エントリー価格 : {signal['entry_price']} USDT")
    print(f"  ・推奨損切りライン(SL) : {signal['stop_loss']} USDT")
    print(f"  ・第1利確ターゲット(TP1): {signal['tp1']} USDT")
    print(f"  ・第2利確ターゲット(TP2): {signal['tp2']} USDT")
    print("=" * 68)


def main() -> None:
    """コマンドライン引数を解析して大循環MACDシステムを実行する。"""
    load_dotenv()
    parser = argparse.ArgumentParser(description="Gate.io × 大循環MACD シグナル＆自動トレード参謀")
    parser.add_argument("contract", nargs="?", default="BTC_USDT", help="通貨ペア (例: BTC_USDT, ETH_USDT, SOL_USDT)")
    parser.add_argument("--watch", type=int, default=0, help="指定した秒数間隔で常時監視してシグナル変化を通知する")
    parser.add_argument("--check-account", action="store_true", help="Gate.io APIキーを使って口座残高・保有ポジション・注文一覧を表示する")
    args = parser.parse_args()

    if args.check_account:
        api_key = os.environ.get("GATE_API_KEY", "")
        api_secret = os.environ.get("GATE_API_SECRET", "")
        if not api_key or not api_secret:
            print("エラー: /Users/apo/Development/.env に GATE_API_KEY と GATE_API_SECRET が設定されていません。")
            sys.exit(1)
        trader = GateTrader(api_key, api_secret)
        account = trader.get_account()
        positions = trader.get_positions()

        print("=== Gate.io 先物口座ステータス ===")
        print(f"総資産(Total): {account.get('total')} USDT / 利用可能(Available): {account.get('available')} USDT")
        print(f"未実現損益(Unrealised PnL): {account.get('unrealised_pnl')} USDT")
        print("\n--- 保有ポジション ---")
        for pos in positions:
            print(
                f"銘柄: {pos.get('contract')} | サイズ: {pos.get('size')} "
                f"| 参入価格: {pos.get('entry_price')} | 現在値: {pos.get('mark_price')} "
                f"| ロスカット価格: {pos.get('liq_price')} | 含み損益: {pos.get('unrealised_pnl')} USDT"
            )
        for c in ["BTC_USDT", "ETH_USDT", "SOL_USDT"]:
            open_orders = trader.get_open_orders(c)
            price_orders = trader.get_price_orders(c)
            if open_orders or price_orders:
                print(f"\n--- {c} の待機中注文 (指値:{len(open_orders)}件 / TP・SL:{len(price_orders)}件) ---")
                for o in open_orders:
                    print(f"  [指値] ID: {o.get('id')} | サイズ: {o.get('size')} | 価格: {o.get('price')} | ReduceOnly: {o.get('is_reduce_only')}")
                for po in price_orders:
                    trig = po.get("trigger", {})
                    rule_str = "以上" if trig.get("rule") == 1 else "以下"
                    print(f"  [トリガー] ID: {po.get('id')} | 条件: {trig.get('price')} {rule_str} | 種別: {po.get('order_type')}")
        return

    decimals = 2 if args.contract != "BTC_USDT" else 1
    last_stage_1h = None
    last_signal_type = None

    while True:
        analyses = {
            tf: fetch_timeframe_analysis(args.contract, tf)
            for tf in ["15m", "1h", "2h", "4h", "1d"]
        }
        signal = evaluate_master_signal(analyses, decimals=decimals)
        print_report(args.contract, analyses, signal)

        if args.watch > 0:
            cur_stage_1h = analyses["1h"].stage
            cur_sig = signal["signal_type"]
            if (last_stage_1h is not None and cur_stage_1h != last_stage_1h) or (
                last_signal_type is not None and cur_sig != last_signal_type
            ):
                send_mac_notification(
                    f"大循環MACD シグナル変化 ({args.contract})",
                    f"{signal['signal_grade']} / 価格: {signal['current_price']}",
                )
            last_stage_1h = cur_stage_1h
            last_signal_type = cur_sig
            time.sleep(args.watch)
        else:
            break


if __name__ == "__main__":
    main()
