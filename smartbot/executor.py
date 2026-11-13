"""منفذ TickerAll للحساب التجريبي.

يستخدم نفس العميل ونفس دوال الحجم المستخدمة في app.py (open_market, positions, spec, closed_profit)،
دون استيراد app.py لتجنب التداخل. الوقف والهدف يُرسلان مع الأمر، فيتولى الوسيط الخروج.

حدود معروفة:
- لا يوجد هنا إغلاق يدوي بالـAPI: مسار الإغلاق اليدوي لم يُتحقق من شكله، لذلك يُرفض صراحة.
- الحماية من الحساب الحقيقي: تُفعّل بمتغير بيئة، ويجب ضبطه على "demo" صراحة.
"""
import os


class LiveGuardError(RuntimeError):
    pass


class TickerAllExecutor:
    def __init__(self, client, lots_fn, mode: str | None = None):
        self.client = client
        self.lots_fn = lots_fn  # دالة app.lots_for_risk(spec, risk_money, sl_distance)
        self.mode = (mode or os.getenv("SMARTBOT_MODE", "")).lower()
        if self.mode != "demo":
            raise LiveGuardError("SMARTBOT_MODE يجب أن يكون 'demo' صراحة. لا تداول على الحساب الحقيقي من هذا المنفذ.")

    def open(self, symbol: str, side: str, entry_ref: float, stop: float, target: float, risk_money: float, comment: str):
        """يفتح صفقة بأمر سوق مع وقف وهدف. يرفض إذا كان هناك مركز على نفس الرمز."""
        if any(p.get("symbol") == symbol for p in self.client.positions()):
            return {"opened": False, "reason": f"يوجد مركز مفتوح على {symbol}"}
        spec = self.client.spec(symbol)
        volume = self.lots_fn(spec, risk_money, abs(entry_ref - stop))
        res = self.client.open_market(symbol, side, volume, stop, target)
        ticket = res.get("ticket")
        if ticket is None:
            raise RuntimeError("لم يُرجع الوسيط رقم الصفقة")
        return {"opened": True, "ticket": ticket, "volume": volume, "comment": comment}

    def check_closed(self, ticket, symbol: str, since_iso: str):
        """يعيد (مغلقة، ربح) — الربح None إذا لم تظهر صفقة الخروج في السجل بعد."""
        still_open = any(str(p.get("ticket")) == str(ticket) for p in self.client.positions())
        if still_open:
            return False, None
        profit = self.client.closed_profit(ticket, since_iso)
        return True, profit

    def close_now(self, ticket):
        raise NotImplementedError("الإغلاق اليدوي بالـAPI لم يُتحقق من شكله بعد؛ الخروج عبر الوقف والهدف المرسلين مع الأمر.")
