"""
Kinetic Revenue Model — Ridge Regression Predictor

Trains per-season Ridge regression models from historical backtest data.
Features are all computable from pre-quarter data (no lookahead bias).

Usage:
    # Train and save models
    python model.py --train

    # Train and evaluate (cross-validation)
    python model.py --eval

    # Show feature importances
    python model.py --features
"""

import argparse
import json
import math
import os
import pickle
import statistics
from collections import defaultdict
from datetime import datetime

import numpy as np
import psycopg2
from dotenv import load_dotenv

from credentials import load_credentials, add_credentials_args
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

load_dotenv()

MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ridge_models.pkl")
MIN_HISTORY = 16  # same warmup as backtest


# ── DB reads ──────────────────────────────────────────────────────────────

def load_all_data():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"), database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"), password=os.getenv("DB_PASSWORD"),
        port=5432, sslmode="require")
    cur = conn.cursor()

    cur.execute("SELECT ticker, period, revenue FROM revenue_actuals ORDER BY ticker, period")
    actuals = defaultdict(list)
    for tk, per, rev in cur.fetchall():
        actuals[tk].append({"period": str(per), "revenue": rev})

    cur.execute("SELECT ticker, period, estimated_revenue FROM pre_earnings_consensus ORDER BY ticker, period")
    pec = defaultdict(dict)
    for tk, per, est in cur.fetchall():
        pec[tk][str(per)] = est

    cur.close()
    conn.close()
    return dict(actuals), dict(pec)


# ── Feature computation ───────────────────────────────────────────────────

def quarter_from_date(s):
    return (datetime.strptime(s, "%Y-%m-%d").month - 1) // 3 + 1


def compute_beat_cadence(actuals_before, pec_before):
    beats = []
    for a in reversed(actuals_before):
        est = pec_before.get(a["period"])
        if est and est > 0:
            beats.append((a["revenue"] - est) / est)
        if len(beats) >= 8:
            break
    if len(beats) < 2:
        return 0.0
    l4 = beats[:4]
    l8 = beats[:8]
    s4 = statistics.stdev(l4) if len(l4) > 1 else float("inf")
    s8 = statistics.stdev(l8) if len(l8) > 1 else float("inf")
    return statistics.mean(l4) if s4 <= s8 else statistics.mean(l8)


def compute_features(actuals_before, pec_for_quarter, pec_all_before, anomaly_periods):
    """Compute the 6 features for a single prediction. Returns dict or None."""
    if len(actuals_before) < 8:
        return None

    prev_rev = actuals_before[-1]["revenue"]
    if prev_rev <= 0:
        return None

    # QoQ series
    qoq_dollar = []
    qoq_pct = []
    for i in range(1, len(actuals_before)):
        prev = actuals_before[i - 1]["revenue"]
        cur = actuals_before[i]["revenue"]
        if prev > 0:
            qoq_dollar.append(cur - prev)
            qoq_pct.append((cur - prev) / prev)

    if len(qoq_dollar) < 4:
        return None

    # Seasonal QoQ for the target quarter
    target_q_num = quarter_from_date(actuals_before[-1]["period"])
    next_q = ((target_q_num - 1 + 1) % 4) + 1
    q_key = f"Q{next_q}"

    seasonal_dollar = []
    seasonal_pct = []
    for i in range(1, len(actuals_before)):
        q = quarter_from_date(actuals_before[i]["period"])
        if q == next_q:
            prev = actuals_before[i - 1]["revenue"]
            cur = actuals_before[i]["revenue"]
            if prev > 0:
                seasonal_dollar.append(cur - prev)
                seasonal_pct.append((cur - prev) / prev)

    if not seasonal_dollar:
        return None

    # Feature 1: $ QoQ projection (trailing seasonal avg)
    s_dollar = seasonal_dollar[-8:]
    f_dollar_qoq = statistics.mean(s_dollar)

    # Feature 2: % QoQ projection (trailing avg % QoQ × current base)
    s_pct = seasonal_pct[-8:]
    avg_pct_qoq = statistics.mean(s_pct)
    f_pct_qoq_projected = prev_rev * avg_pct_qoq

    # Feature 3: Beat-adjusted consensus
    beat_pct = compute_beat_cadence(actuals_before, pec_all_before)
    if pec_for_quarter and pec_for_quarter > 0:
        f_beat_adjusted = pec_for_quarter * (1 + beat_pct)
    else:
        f_beat_adjusted = prev_rev + f_dollar_qoq  # fallback

    # Feature 4: CV of seasonal $ QoQ
    if len(s_dollar) >= 2:
        m = statistics.mean(s_dollar)
        s = statistics.stdev(s_dollar)
        f_cv = abs(s / m) if m != 0 else 0
    else:
        f_cv = 0

    # Feature 5: Revenue base size (log-transformed)
    f_log_rev = math.log(prev_rev)

    # Feature 6: Quarters since last anomaly (0 if no anomaly)
    if anomaly_periods:
        last_anomaly_idx = max(
            (i for i, a in enumerate(actuals_before) if a["period"] in anomaly_periods),
            default=-1)
        f_since_anomaly = len(actuals_before) - 1 - last_anomaly_idx if last_anomaly_idx >= 0 else 20
    else:
        f_since_anomaly = 20  # no anomalies = stable

    return {
        "dollar_qoq": f_dollar_qoq,
        "pct_qoq_projected": f_pct_qoq_projected,
        "beat_adjusted": f_beat_adjusted,
        "cv": f_cv,
        "log_rev": f_log_rev,
        "since_anomaly": f_since_anomaly,
        "quarter": q_key,
    }


def features_to_array(f):
    """Convert feature dict to numpy array."""
    return np.array([
        f["dollar_qoq"],
        f["pct_qoq_projected"],
        f["beat_adjusted"],
        f["cv"],
        f["log_rev"],
        f["since_anomaly"],
    ])


FEATURE_NAMES = [
    "$ QoQ (seasonal avg)",
    "% QoQ projection",
    "Beat-adjusted consensus",
    "CV (volatility)",
    "Log revenue base",
    "Quarters since anomaly",
]


# ── Anomaly detection (simplified, no seasonality needed) ────────────────

def find_anomaly_periods(actuals):
    """Find anomalous periods using 1.5-sigma threshold."""
    if len(actuals) < 8:
        return set()

    qoq = []
    for i in range(1, len(actuals)):
        prev = actuals[i - 1]["revenue"]
        cur = actuals[i]["revenue"]
        q = quarter_from_date(actuals[i]["period"])
        qoq.append({"period": actuals[i]["period"], "quarter": f"Q{q}", "change": cur - prev})

    by_q = defaultdict(list)
    for r in qoq:
        by_q[r["quarter"]].append(r)

    anomalies = set()
    for q, rows in by_q.items():
        changes = [r["change"] for r in rows]
        if len(changes) < 3:
            continue
        avg = statistics.mean(changes)
        std = statistics.stdev(changes)
        if std == 0:
            continue
        for r in rows:
            if abs(r["change"] - avg) / std > 1.5:
                anomalies.add(r["period"])
    return anomalies


# ── Training data builder ─────────────────────────────────────────────────

def build_training_data(all_actuals, all_pec):
    """Build feature matrix and target vector from historical data."""
    samples = {"Q1": [], "Q2": [], "Q3": [], "Q4": []}

    for tk in sorted(all_actuals.keys()):
        actuals = all_actuals[tk]
        pec = all_pec.get(tk, {})

        if len(actuals) < MIN_HISTORY:
            continue

        anomaly_periods = find_anomaly_periods(actuals)
        pec_periods = set(pec.keys())

        for idx in range(MIN_HISTORY, len(actuals)):
            target = actuals[idx]
            target_period = target["period"]

            if target_period not in pec_periods:
                continue

            actuals_before = actuals[:idx]
            pec_for_q = pec[target_period]
            pec_before = {p: v for p, v in pec.items() if p < target_period}

            features = compute_features(actuals_before, pec_for_q, pec_before, anomaly_periods)
            if features is None:
                continue

            q_key = features["quarter"]
            X = features_to_array(features)
            y = target["revenue"]

            samples[q_key].append((X, y, tk, target_period))

    return samples


# ── Model training ────────────────────────────────────────────────────────

def train_models(samples):
    """Train per-season Ridge regression models. Returns dict of (model, scaler)."""
    models = {}

    for q in ("Q1", "Q2", "Q3", "Q4"):
        data = samples.get(q, [])
        if len(data) < 10:
            print(f"  {q}: skipped — only {len(data)} samples")
            continue

        X = np.array([s[0] for s in data])
        y = np.array([s[1] for s in data])

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        model = Ridge(alpha=1.0)
        model.fit(X_scaled, y)

        # Training MAPE
        y_pred = model.predict(X_scaled)
        mape = np.mean(np.abs(y_pred - y) / y) * 100

        models[q] = {"model": model, "scaler": scaler}
        print(f"  {q}: {len(data)} samples, training MAPE={mape:.1f}%, R²={model.score(X_scaled, y):.3f}")

    return models


def save_models(models):
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(models, f)
    print(f"\n  Saved models to {MODEL_PATH}")


def load_models():
    if not os.path.exists(MODEL_PATH):
        return None
    with open(MODEL_PATH, "rb") as f:
        return pickle.load(f)


# ── Prediction ────────────────────────────────────────────────────────────

def predict_with_model(models, features):
    """Predict revenue using the trained Ridge model for the target season.
    Returns predicted revenue or None if no model available.
    """
    q_key = features["quarter"]
    entry = models.get(q_key)
    if entry is None:
        return None

    X = features_to_array(features).reshape(1, -1)
    X_scaled = entry["scaler"].transform(X)
    return float(entry["model"].predict(X_scaled)[0])


# ── Cross-validation ──────────────────────────────────────────────────────

def cross_validate(samples, n_folds=5):
    """Time-aware cross-validation: train on earlier data, test on later."""
    print("\n  Cross-validation (time-aware, 5-fold):")

    for q in ("Q1", "Q2", "Q3", "Q4"):
        data = samples.get(q, [])
        if len(data) < 20:
            print(f"  {q}: skipped — only {len(data)} samples")
            continue

        # Sort by period for time-awareness
        data.sort(key=lambda x: x[3])  # sort by period

        fold_size = len(data) // n_folds
        mapes = []

        for fold in range(n_folds):
            test_start = fold * fold_size
            test_end = test_start + fold_size if fold < n_folds - 1 else len(data)

            # Use everything before test_start as train
            if test_start < MIN_HISTORY:
                continue

            train = data[:test_start]
            test = data[test_start:test_end]

            if len(train) < 10 or len(test) < 3:
                continue

            X_train = np.array([s[0] for s in train])
            y_train = np.array([s[1] for s in train])
            X_test = np.array([s[0] for s in test])
            y_test = np.array([s[1] for s in test])

            scaler = StandardScaler()
            X_train_s = scaler.fit_transform(X_train)
            X_test_s = scaler.transform(X_test)

            model = Ridge(alpha=1.0)
            model.fit(X_train_s, y_train)
            y_pred = model.predict(X_test_s)

            fold_mape = np.mean(np.abs(y_pred - y_test) / y_test) * 100
            mapes.append(fold_mape)

        if mapes:
            print(f"  {q}: CV MAPE = {np.mean(mapes):.1f}% (±{np.std(mapes):.1f}%) across {len(mapes)} folds")


def show_features(models):
    """Show feature importances (Ridge coefficients)."""
    print("\n  Feature Importances (standardized coefficients):")
    for q in ("Q1", "Q2", "Q3", "Q4"):
        entry = models.get(q)
        if not entry:
            continue
        coefs = entry["model"].coef_
        print(f"\n  {q}:")
        ranked = sorted(zip(FEATURE_NAMES, coefs), key=lambda x: abs(x[1]), reverse=True)
        for name, coef in ranked:
            bar = "+" * int(abs(coef) / max(abs(c) for c in coefs) * 20)
            sign = "+" if coef > 0 else "-"
            print(f"    {name:<28s}  {sign}{abs(coef):>12.0f}  {bar}")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Train Ridge revenue prediction models")
    parser.add_argument("--train", action="store_true", help="Train and save models")
    parser.add_argument("--eval", action="store_true", help="Cross-validate")
    parser.add_argument("--features", action="store_true", help="Show feature importances")
    add_credentials_args(parser)
    args = parser.parse_args()

    load_credentials(secret_name=args.secrets, region=args.region)

    if not any([args.train, args.eval, args.features]):
        args.train = True
        args.eval = True
        args.features = True

    print("Loading data...")
    all_actuals, all_pec = load_all_data()

    print("Building training data...")
    samples = build_training_data(all_actuals, all_pec)
    total = sum(len(v) for v in samples.values())
    print(f"  {total} total samples: " + ", ".join(f"{q}={len(v)}" for q, v in sorted(samples.items())))

    if args.train:
        print("\nTraining models...")
        models = train_models(samples)
        save_models(models)

    if args.eval:
        cross_validate(samples)

    if args.features:
        models = load_models()
        if models:
            show_features(models)


if __name__ == "__main__":
    main()
