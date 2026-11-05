import { type ReactNode, useEffect, useState } from "react";

import type { T } from "~/lib/i18n";
import type { Chrome } from "~/lib/types";

type Phase = Chrome["countdown"]["phase"];
type Clock = { phase: Phase; days: number; hours: number; minutes: number; seconds: number };

const UNITS = ["days", "hours", "minutes", "seconds"] as const;

/** 此刻的读数：黑五开始前数到美东零点，进行中数到网一结束，之后全是 0（与 Python 的 fmt.remaining 同一规则）。 */
function clockAt(now: number, target: number, end: number): Clock {
  const phase: Phase = now < target ? "before" : now < end ? "during" : "after";
  const total = Math.floor((phase === "before" ? target - now : phase === "during" ? end - now : 0) / 1000);
  return {
    phase,
    days: Math.floor(total / 86400),
    hours: Math.floor((total % 86400) / 3600),
    minutes: Math.floor((total % 3600) / 60),
    seconds: total % 60,
  };
}

/**
 * 首页首屏：通栏的倒计时块（D65、D68）。服务端渲染出后端给的数字（没有脚本也看得到），水合后浏览器每秒按目标时间重算，
 * 首次渲染与服务端一致，不会水合不匹配。aside 是背景装饰（雷达），children 是底部通栏的读数。
 */
export function CountdownBlock({
  countdown,
  t,
  aside,
  children,
}: {
  countdown: Chrome["countdown"];
  t: T;
  aside?: ReactNode;
  children?: ReactNode;
}) {
  const [clock, setClock] = useState<Clock>({
    phase: countdown.phase,
    days: countdown.days,
    hours: countdown.hours,
    minutes: countdown.minutes,
    seconds: countdown.seconds,
  });
  useEffect(() => {
    const target = Date.parse(countdown.target);
    const end = Date.parse(countdown.end);
    const tick = () => setClock(clockAt(Date.now(), target, end));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [countdown.target, countdown.end]);

  const running = clock.phase !== "after";
  return (
    <section className="countdown-block" data-phase={clock.phase} aria-labelledby="countdown-title">
      {aside}
      <div className="cd-inner">
        <div className="cd-head">
          <h2 id="countdown-title">{t(`countdown.title.${clock.phase}`)}</h2>
          {clock.phase !== "after" && <p className="cd-when">{countdown.when[clock.phase]}</p>}
        </div>
        {running && (
          <p className="cd-clock" role="timer">
            {UNITS.map((unit) => (
              <span key={unit} className="cd-cell">
                <b>{String(clock[unit]).padStart(2, "0")}</b>
                <small>{t(`countdown.unit.${unit}`)}</small>
              </span>
            ))}
          </p>
        )}
      </div>
      {children}
    </section>
  );
}
