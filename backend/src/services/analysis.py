from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction


MAX_TICKETS = 100_000
MAX_PRICE = 1_000_000  # 分
MAX_AMOUNT = MAX_TICKETS * MAX_PRICE
BRIDGE_TERMS = ('票量贡献', '组合贡献', '价格贡献', '退款贡献')
DISPLAY_STEPS = {'元（保留分）': 1, '元（取整）': 100}


def _integer(value, label, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label}须为 {minimum} 至 {maximum} 的整数')
    return value


def _iso_day(value, label):
    try:
        if date.fromisoformat(value).isoformat() == value:
            return value
    except (TypeError, ValueError):
        pass
    raise ValueError(f'{label}须为有效日期，格式为 YYYY-MM-DD')


def _name(value, label):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 300:
        raise ValueError(f'{label}须填写 1 至 300 个字符')
    return value.strip()


@dataclass(frozen=True)
class TicketLine:
    day: str
    performance: str
    channel: str
    tier: str
    plan_tickets: int
    plan_price: int | None
    plan_refund: int
    actual_tickets: int
    actual_gross: int
    actual_refund: int

    @property
    def plan_gross(self):
        return self.plan_tickets * (self.plan_price or 0)

    @property
    def reference_price(self):
        if self.plan_price is not None:
            return self.plan_price
        return Fraction(self.actual_gross, self.actual_tickets) if self.actual_tickets else 0


def _ledger(rows):
    if not rows:
        raise ValueError('票房台账至少需要一行')
    lines = []
    seen = set()
    for number, row in enumerate(rows, 1):
        try:
            key = (
                _iso_day(row.get('report_day'), '演出营业日'),
                _name(row.get('performance'), '场次'),
                _name(row.get('channel'), '票务渠道'),
                _name(row.get('tier'), '票档'),
            )
            if key in seen:
                raise ValueError(f'营业日、场次、渠道和票档重复（{" / ".join(key)}）')
            planned = _integer(row.get('plan_tickets'), '计划票量', 0, MAX_TICKETS)
            actual = _integer(row.get('actual_tickets'), '出票量', 0, MAX_TICKETS)
            price = row.get('plan_price_cents')
            if price is None and planned:
                raise ValueError('有计划票量时必须填写计划单价，赠票请填 0')
            if price is not None:
                price = _integer(price, '计划单价（分）', 0, MAX_PRICE)
            gross = _integer(row.get('actual_gross_cents'), '退款前实收（分）', 0, MAX_AMOUNT)
            if not actual and gross:
                raise ValueError('出票量为 0 时，退款前实收必须为 0')
            if gross > actual * MAX_PRICE:
                raise ValueError('退款前实收的票均单价不得超过 10,000 元')
            plan_refund = _integer(row.get('plan_refund_cents'), '计划退款（分）', 0, MAX_AMOUNT)
            actual_refund = _integer(row.get('actual_refund_cents'), '实退（分）', 0, MAX_AMOUNT)
            line = TicketLine(*key, planned, price, plan_refund, actual, gross, actual_refund)
            if plan_refund > line.plan_gross:
                raise ValueError('计划退款不能超过计划毛票房')
            if actual_refund > gross:
                raise ValueError('实退不能超过退款前实收，请按原出票场次、渠道及票档归集')
        except ValueError as error:
            raise ValueError(f'台账第 {number} 行 · {error}') from None
        seen.add(key)
        lines.append(line)
    return lines


def validate_rows(rows):
    _ledger(rows)


@dataclass
class Bridge:
    plan_gross: int = 0
    actual_gross: int = 0
    plan_refund: int = 0
    actual_refund: int = 0
    volume: Fraction = field(default_factory=Fraction)
    mix: Fraction = field(default_factory=Fraction)
    price: Fraction = field(default_factory=Fraction)

    def add(self, line, plan_total, actual_total):
        # 双边为零时两个等效票量均为零。单边为零时借用另一边的组合份额。
        if plan_total:
            volume_at_plan_mix = Fraction(actual_total * line.plan_tickets, plan_total)
        else:
            volume_at_plan_mix = line.actual_tickets
        reference = line.reference_price
        self.volume += (volume_at_plan_mix - line.plan_tickets) * reference
        self.mix += (line.actual_tickets - volume_at_plan_mix) * reference
        self.price += line.actual_gross - line.actual_tickets * reference
        self.plan_gross += line.plan_gross
        self.actual_gross += line.actual_gross
        self.plan_refund += line.plan_refund
        self.actual_refund += line.actual_refund

    @property
    def plan_net(self):
        return self.plan_gross - self.plan_refund

    @property
    def actual_net(self):
        return self.actual_gross - self.actual_refund

    @property
    def gap(self):
        return self.actual_net - self.plan_net

    @property
    def contributions(self):
        return (self.volume, self.mix, self.price, self.plan_refund - self.actual_refund)

    @property
    def refund_pct(self):
        return Fraction(100 * self.actual_refund, self.actual_gross) if self.actual_gross else None


def _round_steps(cents, step):
    units = Fraction(cents, step)
    magnitude = (2 * abs(units.numerator) + units.denominator) // (2 * units.denominator)
    return magnitude if units >= 0 else -magnitude


def _display_bridge(bridge, step):
    plan, actual = (_round_steps(value, step) for value in (bridge.plan_net, bridge.actual_net))
    effects = [_round_steps(value, step) for value in bridge.contributions]
    gap = actual - plan
    return [plan, actual, *effects, gap - sum(effects), gap]


def _action(bridge, gap_limit, refund_cap):
    if bridge.refund_pct is not None and bridge.refund_pct > refund_cap:
        return '先查退款', '票务主管核对该渠道原单与退票原因，排查期间暂缓加推。'
    if bridge.gap <= -gap_limit:
        return '复核售票', '渠道经理按场次和票档核对销量、配额及折扣，次日报反馈。'
    if bridge.gap >= gap_limit:
        return '维持排期', '保留该渠道当前售票位，核对同场余座后再决定是否加推。'
    return '跟踪', '维持当前配额和投放，随次日报复核。'


def analyze(rows, params):
    lines = _ledger(rows)
    day = _iso_day(params.get('report_day', '2026-11-23'), '日报营业日')
    precision = params.get('display_unit', '元（保留分）')
    if not isinstance(precision, str) or precision not in DISPLAY_STEPS:
        raise ValueError('金额显示精度须选择元（保留分）或元（取整）')
    step = DISPLAY_STEPS[precision]
    gap_limit = _integer(params.get('gap_alert_yuan', 1000), '渠道差异关注线（元）', 1, 1_000_000_000) * 100
    cap = params.get('refund_alert_pct', 10)
    try:
        if isinstance(cap, bool) or not isinstance(cap, (int, float)):
            raise ValueError
        refund_cap = Fraction(str(cap))
        if not 0 <= refund_cap <= 100:
            raise ValueError
    except ValueError:
        raise ValueError('退款率关注线须为 0 至 100 的百分数') from None
    refund_cap_label = str(cap).removesuffix('.0')
    selected = sorted((line for line in lines if line.day == day),
                      key=lambda line: (line.performance, line.channel, line.tier))
    if not selected:
        raise ValueError(f'{day} 没有台账记录，请更换日报营业日或导入对应 CSV')

    plan_total = sum(line.plan_tickets for line in selected)
    actual_total = sum(line.actual_tickets for line in selected)
    total = Bridge()
    channels = defaultdict(Bridge)
    for line in selected:
        total.add(line, plan_total, actual_total)
        channels[line.channel].add(line, plan_total, actual_total)

    def money(units):
        return units if step == 100 else units / 100

    channel_order = sorted(channels, key=lambda channel: (channels[channel].gap, channel))
    total_display = _display_bridge(total, step)
    channel_values = [_display_bridge(channels[channel], step) for channel in channel_order]
    channel_sum = list(map(sum, zip(*channel_values, strict=True)))
    channel_rows = [[channel, *map(money, values)]
                    for channel, values in zip(channel_order, channel_values, strict=True)]
    channel_rows.extend([
        ['各渠道显示合计', *map(money, channel_sum)],
        ['全剧场与各渠道显示合计之差', *[money(a - b) for a, b in zip(total_display, channel_sum, strict=True)]],
    ])
    decisions = []
    action_groups = defaultdict(list)
    for channel in channel_order:
        bridge = channels[channel]
        action, detail = _action(bridge, gap_limit, refund_cap)
        action_groups[action].append(channel)
        rate = bridge.refund_pct
        rate_value = _round_steps(rate * 100, 1) / 100 if rate is not None else '无销售额'
        decisions.append([channel, bridge.gap / 100, rate_value, action, detail])
    attention = []
    for action in ('先查退款', '复核售票', '维持排期'):
        group = action_groups.get(action)
        if group:
            names = '、'.join(group[:2])
            if len(group) > 2:
                names += f'等 {len(group)} 个渠道'
            attention.append(f'{action}（{names}）')
    summary = (
        f'{day} 剧场实收 {total.actual_net / 100:,.2f} 元，计划 {total.plan_net / 100:,.2f} 元，'
        f'相差 {total.gap / 100:+,.2f} 元。'
        + (f'建议{"、".join(attention)}。' if attention else '各渠道维持当前配额和投放，随次日报复核。')
    )
    bridge_labels = ('计划净票房', '实际净实收', *BRIDGE_TERMS, '舍入尾差', '净差异')
    explanations = (
        '计划票量乘计划单价，再减计划退款', '退款前实收减实退',
        '保持全剧场计划组合份额和基准单价，替换总票量',
        '保持实际总票量和基准单价，替换各明细实际组合份额',
        '比较实际出票量按基准单价的毛额与退款前实收',
        '计划退款减实退', '显示净差异减四项显示贡献之和', '显示实际净实收减显示计划净票房',
    )
    unit_label = '金额（元，取整）' if step == 100 else '金额（元，保留至分）'
    bridge_rows = [[name, money(value), detail] for name, value, detail in
                   zip(bridge_labels, total_display, explanations, strict=True)]
    return {
        'summary': summary,
        'metrics': [
            {'label': '计划票量', 'value': plan_total, 'detail': '张，含计划赠票'},
            {'label': '实际出票', 'value': actual_total, 'detail': '张，退款前，含赠票'},
            {'label': '计划净票房（元）', 'value': money(total_display[0]), 'detail': precision},
            {'label': '实际净实收（元）', 'value': money(total_display[1]), 'detail': precision},
            {'label': '显示净差异（元）', 'value': money(total_display[-1]), 'detail': '显示实际净实收减显示计划净票房'},
            {'label': '舍入尾差（元）', 'value': money(total_display[-2]), 'detail': '计入后四项贡献合计等于显示净差异'},
        ],
        'tables': [
            {'title': '全剧场计划到实收桥接', 'columns': ['项目', unit_label, '计算口径'], 'rows': bridge_rows},
            {'title': f'渠道贡献与跨渠道舍入对账 · {precision}', 'columns': ['渠道', *bridge_labels],
             'rows': channel_rows},
            {'title': '渠道处置建议', 'columns': ['渠道', '精确净差异（元）', '退款率（%，显示两位）', '处置', '下一步'],
             'rows': decisions},
            {'title': f'复算底稿 · {day}',
             'columns': ['场次', '渠道', '票档', '计划票量（张）', '计划单价（分）', '计划退款（分）',
                         '出票量（张）', '退款前实收（分）', '实退（分）'],
             'rows': [[line.performance, line.channel, line.tier, line.plan_tickets, line.plan_price,
                       line.plan_refund, line.actual_tickets, line.actual_gross, line.actual_refund] for line in selected]},
        ],
        'notes': [
            f'营业日 {day}，纳入 {len(selected)} 行。退款以原演出场次、渠道和票档归集，底稿金额以分计。',
            '按票量、组合、价格、退款顺序桥接。每行先保留精确分值（含分数），再分别汇总全剧场和渠道。',
            '计划票量为零且计划单价留空时，基准单价用该行实际票均实收。任一侧全剧场总票量为零时，两侧组合采用有票一侧的份额。',
            '四舍五入时正负半单位均远离零。显示净差异用已显示的实际净实收减计划净票房。渠道桥接的最后一行列出分别舍入造成的汇总差。',
            f'渠道处置按原始精度比较。净差异关注线为 {gap_limit // 100:,} 元，退款率超过 {refund_cap_label}% 时优先查退款。',
        ],
        'series_title': f'净实收差异分解 · {precision}',
        'series': [{'label': label, 'value': money(value)}
                   for label, value in zip(bridge_labels[2:-1], total_display[2:-1], strict=True)],
    }
