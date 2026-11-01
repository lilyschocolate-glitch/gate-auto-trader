#!/usr/bin/env python3
"""Gate.io × 大循環MACD 24時間自動トレード＆複利ポジション管理ボット。

動作内容:
1. 口座の総資産（Total Equity）を取得し、資金増加に合わせて複利でロットサイズを自動計算する。
2. 対象銘柄（BTC_USDT 200x, ETH_USDT 200x, SOL_USDT 100x）のレバレッジを常に最高倍率に保つ。
3. 古い未約定注文や決済後の残りトリガー注文を自動でお掃除・最新EMA価格へ更新する。
4. 大循環MACDのステージと3本のMACDの傾きを監視し、ロング・ショート両方向で自動発注・TP/SL管理を行う。
"""

import os
import time
from datetime import datetime
from daijunkan_macd import (
    CONTRACT_SPECS,
    GateTrader,
    evaluate_master_signal,
    fetch_timeframe_analysis,
    load_dotenv,
)


def log(msg: str) -> None:
    """タイムスタンプ付きでログを出力する。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def cleanup_contract_orders(trader: GateTrader, contract: str) -> None:
    """指定銘柄の未約定指値注文およびトリガー注文をすべてキャンセルして綺麗にする。"""
    trader._request("DELETE", "/api/v4/futures/usdt/orders", f"contract={contract}")
    trader._request("DELETE", "/api/v4/futures/usdt/price_orders", f"contract={contract}")


def ensure_orders_for_contract(trader: GateTrader, contract: str, total_equity: float) -> None:
    """銘柄ごとの大循環MACDシグナルを判定し、注文更新および保有ポジションのTP/SL管理を行う。"""
    spec = CONTRACT_SPECS[contract]
    decimals = 2 if contract != "BTC_USDT" else 1

    analyses = {
        tf: fetch_timeframe_analysis(contract, tf)
        for tf in ["15m", "1h", "2h", "4h", "1d"]
    }
    signal = evaluate_master_signal(analyses, decimals=decimals)
    tf_1h = analyses["1h"]
    tf_2h = analyses["2h"]
    price = signal["current_price"]

    positions = [p for p in trader.get_positions() if p.get("contract") == contract]
    open_orders = trader.get_open_orders(contract)
    price_orders = trader.get_price_orders(contract)

    # 1. ポジション保有中の場合：証拠金バッファ・TP（利確）・SL（損切り）を管理
    if positions:
        pos = positions[0]
        size = int(pos.get("size", 0))
        entry_price = float(pos.get("entry_price", price))
        liq_price = float(pos.get("liq_price", 0.0))
        is_short = size < 0

        # 分離マージンのロスカット価格がSLより手前に来ないよう、自動で証拠金バッファを追加
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

        if not price_orders:
            sl_price = signal["stop_loss"]
            if is_short and sl_price <= price:
                sl_price = round(price + tf_1h.atr * 0.8, decimals)
            elif not is_short and sl_price >= price:
                sl_price = round(price - tf_1h.atr * 0.8, decimals)

            rule = 1 if is_short else 2
            order_type = "close-short-position" if is_short else "close-long-position"
            trader.place_price_trigger_order(contract, sl_price, rule, order_type)
            log(f"[{contract}] 保有ポジション({size}枚)に自動損切り(SL)をセット: {sl_price}")

        reduce_orders = [o for o in open_orders if o.get("is_reduce_only")]
        if not reduce_orders:
            tp_price = signal["tp1"]
            if is_short and tp_price >= entry_price:
                tp_price = round(entry_price - tf_1h.atr * 1.5, decimals)
            elif not is_short and tp_price <= entry_price:
                tp_price = round(entry_price + tf_1h.atr * 1.5, decimals)

            trader.place_limit_order(contract, -size, tp_price, reduce_only=True)
            log(f"[{contract}] 保有ポジション({size}枚)に自動利確指値(TP)をセット: {tp_price}")

        return

    # 2. ポジション未保有時：古い注文・不要な残りトリガーのお掃除判定
    entry_orders = [o for o in open_orders if not o.get("is_reduce_only")]

    # ポジションも新規指値もないのに古いトリガーだけ残っている場合は削除
    if not entry_orders and price_orders:
        cleanup_contract_orders(trader, contract)
        log(f"[{contract}] 決済後に残っていた古いトリガー注文({len(price_orders)}件)を自動削除しました")
        price_orders = []

    sig_type = signal["signal_type"]
    want_short = sig_type in ("SHORT_NOW", "SHORT_LIMIT_WAIT")
    want_long = sig_type in ("LONG_NOW", "LONG_LIMIT_WAIT")

    if want_short:
        band_price = round(max(tf_1h.ema20, min(tf_1h.ema40, tf_2h.ema20)), decimals)
        target_entry_p = max(round(price + tf_1h.atr * 0.15, decimals), band_price)
    elif want_long:
        band_price = round(min(tf_1h.ema20, max(tf_1h.ema40, tf_2h.ema20)), decimals)
        target_entry_p = min(round(price - tf_1h.atr * 0.15, decimals), band_price)
    else:
        target_entry_p = signal["entry_price"]

    # 待機中の古い指値注文がある場合、方向転換またはEMAからの乖離をチェックして自動更新
    if entry_orders:
        old_order = entry_orders[0]
        old_price = float(old_order.get("price", 0.0))
        old_size = int(old_order.get("size", 0))
        old_is_short = old_size < 0

        # 様子見に変わった、または売買方向が変わった、または目標EMAラインから0.35ATR以上ズレたら古い注文を取り消す
        price_diff = abs(old_price - target_entry_p)
        if (
            (not want_short and not want_long)
            or (want_short and not old_is_short)
            or (want_long and old_is_short)
            or (price_diff > tf_1h.atr * 0.35)
        ):
            cleanup_contract_orders(trader, contract)
            log(
                f"[{contract}] 古い指値注文({old_price})が最新の目標EMAライン({target_entry_p})から"
                f"ズレたため自動キャンセルして更新します"
            )
        else:
            log(
                f"[{contract}] 現在値={price} | 1h={tf_1h.stage_name} | "
                f"有効な指値注文({old_price})を維持中"
            )
            return

    if not want_short and not want_long:
        log(f"[{contract}] 現在値={price} | 判定={signal['signal_grade']} | 新規注文なし（待機）")
        return

    # 3. 最新のEMAラインに新規指値注文＋TP/SLトリガーをセット
    trader.set_max_leverage(contract)
    mult = 15.0 if contract == "BTC_USDT" else (12.0 if contract == "ETH_USDT" else 10.0)
    notional = total_equity * mult
    contracts = max(int(round((notional / price) / spec["multiplier"])), 1)

    if want_short:
        band_price = round(max(tf_1h.ema20, min(tf_1h.ema40, tf_2h.ema20)), decimals)
        entry_p = max(round(price + tf_1h.atr * 0.15, decimals), band_price)
        sl_p = max(round(entry_p + tf_1h.atr * 0.65, decimals), round(price + tf_1h.atr * 0.4, decimals))
        tp_p = min(round(entry_p - tf_1h.atr * 1.6, decimals), round(price - tf_1h.atr * 0.5, decimals))

        trader.place_limit_order(contract, -contracts, entry_p, reduce_only=False)
        trader._request(
            "POST",
            "/api/v4/futures/usdt/price_orders",
            payload={
                "initial": {"contract": contract, "size": contracts, "price": "0", "tif": "ioc", "reduce_only": True},
                "trigger": {"strategy_type": 0, "price_type": 0, "price": str(sl_p), "rule": 1, "expiration": 86400 * 3},
            },
        )
        trader._request(
            "POST",
            "/api/v4/futures/usdt/price_orders",
            payload={
                "initial": {"contract": contract, "size": contracts, "price": "0", "tif": "ioc", "reduce_only": True},
                "trigger": {"strategy_type": 0, "price_type": 0, "price": str(tp_p), "rule": 2, "expiration": 86400 * 3},
            },
        )
        log(f"[{contract}] 最新ショート指値を配置: -{contracts}枚 @ {entry_p} (SL:{sl_p} / TP:{tp_p})")

    elif want_long:
        band_price = round(min(tf_1h.ema20, max(tf_1h.ema40, tf_2h.ema20)), decimals)
        entry_p = min(round(price - tf_1h.atr * 0.15, decimals), band_price)
        sl_p = min(round(entry_p - tf_1h.atr * 0.65, decimals), round(price - tf_1h.atr * 0.4, decimals))
        tp_p = max(round(entry_p + tf_1h.atr * 1.6, decimals), round(price + tf_1h.atr * 0.5, decimals))

        trader.place_limit_order(contract, contracts, entry_p, reduce_only=False)
        trader._request(
            "POST",
            "/api/v4/futures/usdt/price_orders",
            payload={
                "initial": {"contract": contract, "size": contracts, "price": "0", "tif": "ioc", "reduce_only": True},
                "trigger": {"strategy_type": 0, "price_type": 0, "price": str(sl_p), "rule": 2, "expiration": 86400 * 3},
            },
        )
        trader._request(
            "POST",
            "/api/v4/futures/usdt/price_orders",
            payload={
                "initial": {"contract": contract, "size": contracts, "price": "0", "tif": "ioc", "reduce_only": True},
                "trigger": {"strategy_type": 0, "price_type": 0, "price": str(tp_p), "rule": 1, "expiration": 86400 * 3},
            },
        )
        log(f"[{contract}] 最新ロング指値を配置: +{contracts}枚 @ {entry_p} (SL:{sl_p} / TP:{tp_p})")


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
