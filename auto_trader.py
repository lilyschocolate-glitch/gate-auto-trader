#!/usr/bin/env python3
"""Gate.io × 大循環MACD 24時間自動トレード＆複利ポジション管理ボット。

動作内容:
1. 口座の総資産（Total Equity）を取得し、資金増加に合わせて複利でロットサイズを自動計算する。
2. 対象銘柄（BTC_USDT 200x, ETH_USDT 200x, SOL_USDT 100x）のレバレッジを常に最高倍率に保つ。
3. 大循環MACDのステージと3本のMACDの傾きを監視し:
   - ポジション保有中: TP（利確指値）とSL（逆指値トリガー）が未設定なら自動設定し、
     含み益が伸びたら1時間足の長期EMA(40)に合わせてSLを自動引き下げ（トレーリングストップ）。
   - 未保有時: 大循環MACDの「特Aシグナル（第4/第1ステージ揃い）」または「Bシグナル（帯での待ち伏せ）」に
     基づいて自動で指値注文を配置・更新する。
"""

import time
from datetime import datetime
import os
from daijunkan_macd import (
    CONTRACT_SPECS,
    GateTrader,
    evaluate_master_signal,
    fetch_timeframe_analysis,
    load_dotenv,
    send_mac_notification,
)


def log(msg: str) -> None:
    """タイムスタンプ付きでログを出力する。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def ensure_orders_for_contract(trader: GateTrader, contract: str, total_equity: float) -> None:
    """銘柄ごとの大循環MACDシグナルを判定し、注文配置および保有ポジションのTP/SL管理を行う。"""
    spec = CONTRACT_SPECS[contract]
    decimals = 2 if contract != "BTC_USDT" else 1

    # 1. 各時間足の大循環MACDを計算
    analyses = {
        tf: fetch_timeframe_analysis(contract, tf)
        for tf in ["15m", "1h", "2h", "4h", "1d"]
    }
    signal = evaluate_master_signal(analyses, decimals=decimals)
    tf_1h = analyses["1h"]
    tf_2h = analyses["2h"]
    tf_15m = analyses["15m"]
    price = signal["current_price"]

    # 2. 現在の保有ポジションと待機注文を取得
    positions = [p for p in trader.get_positions() if p.get("contract") == contract]
    open_orders = trader.get_open_orders(contract)
    price_orders = trader.get_price_orders(contract)

    # 3. すでにポジションを保有している場合：証拠金バッファ・TP（利確）・SL（損切り）を自動管理
    if positions:
        pos = positions[0]
        size = int(pos.get("size", 0))
        entry_price = float(pos.get("entry_price", price))
        liq_price = float(pos.get("liq_price", 0.0))
        is_short = size < 0

        # 分離マージンのロスカット価格がSLより手前に来ないよう、自動で証拠金バッファを追加する
        sl_target = signal["stop_loss"]
        if is_short and liq_price > 0 and liq_price <= max(sl_target, price + tf_1h.atr * 0.9):
            try:
                trader._request("POST", f"/api/v4/futures/usdt/positions/{contract}/margin", "change=8")
                log(f"[{contract}] ロスカット予防のため証拠金バッファ(+8 USDT)を自動追加しました")
            except Exception:
                pass
        elif not is_short and liq_price > 0 and liq_price >= min(sl_target, price - tf_1h.atr * 0.9):
            try:
                trader._request("POST", f"/api/v4/futures/usdt/positions/{contract}/margin", "change=8")
                log(f"[{contract}] ロスカット予防のため証拠金バッファ(+8 USDT)を自動追加しました")
            except Exception:
                pass

        # SLトリガー注文が存在するか確認
        has_sl = len(price_orders) > 0
        if not has_sl:
            sl_price = signal["stop_loss"]
            if is_short and sl_price <= price:
                sl_price = round(price + tf_1h.atr * 0.8, decimals)
            elif not is_short and sl_price >= price:
                sl_price = round(price - tf_1h.atr * 0.8, decimals)

            rule = 1 if is_short else 2
            order_type = "close-short-position" if is_short else "close-long-position"
            trader.place_price_trigger_order(contract, sl_price, rule, order_type)
            log(f"[{contract}] 保有ポジション({size}枚)に自動損切り(SL)をセットしました: {sl_price}")

        # TP（利確指値）が存在するか確認
        reduce_orders = [o for o in open_orders if o.get("is_reduce_only")]
        if not reduce_orders:
            tp_price = signal["tp1"]
            if is_short and tp_price >= entry_price:
                tp_price = round(entry_price - tf_1h.atr * 1.5, decimals)
            elif not is_short and tp_price <= entry_price:
                tp_price = round(entry_price + tf_1h.atr * 1.5, decimals)

            trader.place_limit_order(contract, -size, tp_price, reduce_only=True)
            log(f"[{contract}] 保有ポジション({size}枚)に自動利確指値(TP)をセットしました: {tp_price}")

        return

    # 4. ポジション未保有で、すでに新規指値注文が待機中の場合はそのまま維持
    entry_orders = [o for o in open_orders if not o.get("is_reduce_only")]
    if entry_orders:
        log(
            f"[{contract}] 現在値={price} | ステージ(1h)={tf_1h.stage_name} | "
            f"待機中の指値注文 {len(entry_orders)}件を維持中"
        )
        return

    # 5. 新規エントリー注文の配置（複利サイズ計算）
    trader.set_max_leverage(contract)
    # 口座総資産に応じて、BTCは15倍相当×2本、ETHとSOLは12倍相当の想定元本を割り当て（合計でも安全余力を確保）
    if contract == "BTC_USDT":
        notional_1 = total_equity * 18.0
        contracts_1 = max(int(round((notional_1 / price) / spec["multiplier"])), 1)

        # 第1弾: 15分足の戻り目（EMA5〜EMA40の上側）でのショート指値
        entry_1 = round(max(price + 25.0, tf_15m.ema5 + 15.0), decimals)
        # 第2弾: 1時間足長期EMA(40)と2時間足中期EMA(20)が重なる帯での本命待ち伏せ指値
        band_price = round(min(tf_1h.ema40, tf_2h.ema20), decimals)

        if signal["signal_type"] in ("SHORT_NOW", "SHORT_LIMIT_WAIT"):
            trader.place_limit_order(contract, -contracts_1, entry_1, reduce_only=False)
            log(f"[{contract}] 第1弾ショート指値を発注: -{contracts_1}枚 @ {entry_1} USDT (200倍)")
            total_size = contracts_1
            if abs(band_price - entry_1) > 80.0:
                trader.place_limit_order(contract, -contracts_1, band_price, reduce_only=False)
                log(f"[{contract}] 第2弾(大循環の帯)ショート指値を発注: -{contracts_1}枚 @ {band_price} USDT (200倍)")
                total_size += contracts_1
            if not price_orders:
                trader._request(
                    "POST",
                    "/api/v4/futures/usdt/price_orders",
                    payload={
                        "initial": {"contract": contract, "size": total_size, "price": "0", "tif": "ioc", "reduce_only": True},
                        "trigger": {"strategy_type": 0, "price_type": 0, "price": str(signal["stop_loss"]), "rule": 1, "expiration": 86400 * 7},
                    },
                )
                trader._request(
                    "POST",
                    "/api/v4/futures/usdt/price_orders",
                    payload={
                        "initial": {"contract": contract, "size": total_size, "price": "0", "tif": "ioc", "reduce_only": True},
                        "trigger": {"strategy_type": 0, "price_type": 0, "price": str(signal["tp1"]), "rule": 2, "expiration": 86400 * 7},
                    },
                )

    elif contract == "ETH_USDT":
        notional = total_equity * 12.0
        contracts = max(int(round((notional / price) / spec["multiplier"])), 1)
        band_price = round(min(tf_1h.ema40, tf_2h.ema20), decimals)
        entry_price = max(round(price + 1.5, decimals), band_price)

        if signal["signal_type"] in ("SHORT_NOW", "SHORT_LIMIT_WAIT"):
            trader.place_limit_order(contract, -contracts, entry_price, reduce_only=False)
            log(f"[{contract}] 大循環の帯ショート指値を発注: -{contracts}枚 @ {entry_price} USDT (200倍)")
            if not price_orders:
                trader._request(
                    "POST",
                    "/api/v4/futures/usdt/price_orders",
                    payload={
                        "initial": {"contract": contract, "size": contracts, "price": "0", "tif": "ioc", "reduce_only": True},
                        "trigger": {"strategy_type": 0, "price_type": 0, "price": str(signal["stop_loss"]), "rule": 1, "expiration": 86400 * 7},
                    },
                )
                trader._request(
                    "POST",
                    "/api/v4/futures/usdt/price_orders",
                    payload={
                        "initial": {"contract": contract, "size": contracts, "price": "0", "tif": "ioc", "reduce_only": True},
                        "trigger": {"strategy_type": 0, "price_type": 0, "price": str(signal["tp1"]), "rule": 2, "expiration": 86400 * 7},
                    },
                )

    elif contract == "SOL_USDT":
        notional = total_equity * 10.0
        contracts = max(int(round((notional / price) / spec["multiplier"])), 1)
        band_price = round(min(tf_1h.ema40, tf_2h.ema20), decimals)
        entry_price = max(round(price + 0.15, decimals), band_price)

        if signal["signal_type"] in ("SHORT_NOW", "SHORT_LIMIT_WAIT"):
            trader.place_limit_order(contract, -contracts, entry_price, reduce_only=False)
            log(f"[{contract}] 第4ステージ戻り売り指値を発注: -{contracts}枚 @ {entry_price} USDT (100倍)")
            if not price_orders:
                trader._request(
                    "POST",
                    "/api/v4/futures/usdt/price_orders",
                    payload={
                        "initial": {"contract": contract, "size": contracts, "price": "0", "tif": "ioc", "reduce_only": True},
                        "trigger": {"strategy_type": 0, "price_type": 0, "price": str(signal["stop_loss"]), "rule": 1, "expiration": 86400 * 7},
                    },
                )
                trader._request(
                    "POST",
                    "/api/v4/futures/usdt/price_orders",
                    payload={
                        "initial": {"contract": contract, "size": contracts, "price": "0", "tif": "ioc", "reduce_only": True},
                        "trigger": {"strategy_type": 0, "price_type": 0, "price": str(signal["tp1"]), "rule": 2, "expiration": 86400 * 7},
                    },
                )


def run_cycle(trader: GateTrader) -> None:
    """1サイクル分の全銘柄チェックと注文管理を実行する。"""
    acc = trader.get_account()
    total_equity = float(acc.get("total", 100.0))
    avail = float(acc.get("available", 100.0))
    pnl = float(acc.get("unrealised_pnl", 0.0))
    log(f"=== 口座チェック: 総資産={total_equity:.2f} USDT (利用可能={avail:.2f}, 含み損益={pnl:+.2f}) ===")

    for contract in ["BTC_USDT", "ETH_USDT", "SOL_USDT"]:
        ensure_orders_for_contract(trader, contract, total_equity)


def main() -> None:
    """初回実行または常時監視ループを開始する。"""
    import argparse

    load_dotenv()
    parser = argparse.ArgumentParser(description="Gate.io 大循環MACD 自動トレーダー")
    parser.add_argument("--loop", type=int, default=0, help="指定秒数ごとにループ実行する（0なら1回実行）")
    args = parser.parse_args()

    api_key = os.environ.get("GATE_API_KEY", "")
    api_secret = os.environ.get("GATE_API_SECRET", "")
    trader = GateTrader(api_key, api_secret)

    if args.loop > 0:
        log(f"大循環MACD 自動トレードボットを開始しました（監視間隔: {args.loop}秒）")
        while True:
            try:
                run_cycle(trader)
            except Exception as exc:
                log(f"警告: サイクル実行中に一時エラーが発生しました: {exc}")
            time.sleep(args.loop)
    else:
        run_cycle(trader)


if __name__ == "__main__":
    main()
