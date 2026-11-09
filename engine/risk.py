"""حمايات ثابتة. كل دالة نقية: تأخذ أرقاماً وتعيد قراراً."""
import os

GOLD_SYMBOL = "XAUUSD"
GOLD_MIN_BALANCE = float(os.getenv("GOLD_MIN_BALANCE", "500"))
ZERO_BALANCE_EPS = 1.0
DEFAULT_RISK_PCT = 1.0  # نسبة الرصيد المخاطَر في الصفقة الواحدة


def blocked_reason(symbol: str, balance: float):
    """يعيد سبب المنع، أو None إذا كان التداول مسموحاً."""
    if symbol == GOLD_SYMBOL and balance < GOLD_MIN_BALANCE:
        return (f"الذهب محظور حتى يبلغ الرصيد {GOLD_MIN_BALANCE:g}$ "
                f"(الرصيد الحالي {balance:,.2f}$)")
    return None


def is_zero_balance(balance: float) -> bool:
    return balance <= ZERO_BALANCE_EPS


def risk_amount(balance: float, risk_pct: float = DEFAULT_RISK_PCT) -> float:
    """المبلغ المسموح خسارته في صفقة واحدة. لا يُسمح بنسبة أعلى من 5%."""
    if not 0 < risk_pct <= 5:
        raise ValueError("نسبة المخاطرة يجب أن تكون بين 0 و5%")
    return round(max(balance, 0.0) * risk_pct / 100, 2)
