import { useState } from "react";
import { useMoney } from "./Amount";

export interface CashPoint {
  /** Axis label, e.g. "Sep". */
  label: string;
  /** An exact decimal string. It is only ever formatted, never summed here. */
  value: string;
}

const W = 380;
const H = 176;
const LEFT = 28;
const BASE = 140;
const TOP = 37.1;
const STEP_Y = (BASE - TOP) / 3;

/** Geometry helper: 1, 2, 2.5, 5 x 10^n at or above `raw`. Not a money calculation. */
function niceStep(raw: number): number {
  const power = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * power >= raw) return m * power;
  return 10 * power;
}

function axisLabel(v: number): string {
  return Math.abs(v) >= 1000 ? `${Math.round(v / 1000)}k` : String(Math.round(v));
}

/**
 * The cash-position chart (section 5, Chart): one series, a 2px brand line, a
 * 16% brand area, hairline gridlines at four ticks, mono y labels. The last
 * point is marked by default; hovering or focusing a column moves the
 * crosshair, dot and tooltip. There is a text summary and a table alternative.
 *
 * The scale below is drawing geometry only; every figure shown to the reader is
 * the original decimal string passed through the exact formatter.
 */
export function CashChart({ series, summary }: { series: readonly CashPoint[]; summary: string }) {
  const money = useMoney();
  const [hover, setHover] = useState<number | null>(null);
  if (series.length === 0) return null;

  const nums = series.map((p) => Number(p.value));
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  let step = niceStep(Math.max((max - min) / 3, 1));
  let lo = Math.floor(min / step) * step;
  while (lo + 3 * step < max) {
    step = niceStep(step * 1.01);
    lo = Math.floor(min / step) * step;
  }
  const yOf = (v: number) => BASE - ((v - lo) / step) * STEP_Y;
  const span = W - LEFT - 40;
  const xOf = (i: number) =>
    LEFT + 12 + (series.length === 1 ? span / 2 : (i * span) / (series.length - 1));

  const active = hover ?? series.length - 1;
  const points = series.map((_, i) => `${xOf(i).toFixed(1)} ${yOf(nums[i] ?? 0).toFixed(1)}`);
  const line = `M${points.join(" L")}`;
  const area = `${line} L${xOf(series.length - 1).toFixed(1)} ${BASE} L${xOf(0).toFixed(1)} ${BASE} Z`;
  const ax = xOf(active);
  const ay = yOf(nums[active] ?? 0);
  const tipLeft = Math.min(Math.max(ax - 98, 0), W - 130);
  const pct = (n: number, of: number) => `${(n / of) * 100}%`;
  const columnWidth = series.length === 1 ? span : span / (series.length - 1);

  return (
    <div className="ui-chart" data-testid="cash-chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary}>
        <g stroke="var(--border-subtle)" strokeWidth="1">
          {[0, 1, 2, 3].map((k) => (
            <line key={k} x1={LEFT} x2={W} y1={BASE - k * STEP_Y} y2={BASE - k * STEP_Y} />
          ))}
        </g>
        <g fontFamily="var(--font-mono)" fontSize="11" fill="var(--ink-muted)">
          {[0, 1, 2, 3].map((k) => (
            <text key={k} x="0" y={BASE - k * STEP_Y + 4}>
              {axisLabel(lo + k * step)}
            </text>
          ))}
        </g>
        <path d={area} fill="var(--brand)" fillOpacity="0.16" />
        <path
          d={line}
          fill="none"
          stroke="var(--brand)"
          strokeWidth="2"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
        <g fontFamily="var(--font-sans)" fontSize="12" fill="var(--ink-muted)" textAnchor="middle">
          {series.map((p, i) => (
            <text key={p.label + i} x={xOf(i)} y="164">
              {p.label}
            </text>
          ))}
        </g>
      </svg>
      <div className="ui-chart__rule" style={{ left: pct(ax, W), height: pct(BASE, H) }} />
      <div className="ui-chart__dot" style={{ left: pct(ax, W), top: pct(ay, H) }} />
      <div className="ui-chart__tip" style={{ left: pct(tipLeft, W), top: 0 }}>
        {series[active]?.label} · {money(series[active]?.value ?? "0")}
      </div>
      {series.map((p, i) => (
        <button
          key={p.label + i}
          type="button"
          className="ui-chart__hit"
          style={{ left: pct(xOf(i) - columnWidth / 2, W), width: pct(columnWidth, W) }}
          aria-label={`${p.label}: ${money(p.value)}`}
          onMouseEnter={() => setHover(i)}
          onMouseLeave={() => setHover(null)}
          onFocus={() => setHover(i)}
          onBlur={() => setHover(null)}
        />
      ))}
      {/* Hidden through a wrapper, not on the table itself: a table ignores `width: 1px` and
          `overflow: hidden` and lays out at its content's width, so the "hidden" table sat
          off to the right of the dashboard and made the whole page scroll sideways. */}
      <div className="ui-visually-hidden">
        <table>
          <caption>{summary}</caption>
          <tbody>
            {series.map((p, i) => (
              <tr key={p.label + i}>
                <th scope="row">{p.label}</th>
                <td>{money(p.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
