import { Form, Link, useSubmit } from "react-router";

import type { Route } from "./+types/plans";
import { filterHandlers, Pager, ScopeTabs } from "~/components/common";
import { CopyCode } from "~/components/copy-code";
import { Button } from "~/components/ui/button";
import { Input } from "~/components/ui/input";
import { NativeSelect, NativeSelectOption } from "~/components/ui/native-select";
import { internal } from "~/lib/api.server";
import { langOf, type T } from "~/lib/i18n";
import { seoMeta } from "~/lib/seo";
import type { Option, PlansPage } from "~/lib/types";
import { useT } from "~/lib/use-t";

export async function loader({ request, url }: Route.LoaderArgs) {
  return internal<PlansPage>(`/${langOf(url.pathname)}/plans${url.search}`, request.signal);
}

export const meta: Route.MetaFunction = ({ loaderData }) => seoMeta(loaderData?.seo);

/** 带"不限"空选项的原生下拉。 */
function FilterSelect({ name, label, options, any }: { name: string; label: string; options: Option[]; any: string }) {
  return (
    <label className="filter-field">
      <span>{label}</span>
      <NativeSelect name={name} defaultValue={options.find((o) => o.selected)?.value ?? ""} className="w-full">
        <NativeSelectOption value="">{any}</NativeSelectOption>
        {options.map((o) => (
          <NativeSelectOption key={o.value} value={o.value}>
            {o.label}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </label>
  );
}

function PlanFilters({ form, total, t }: { form: PlansPage["form"]; total: string; t: T }) {
  const submit = useSubmit();
  const any = t("plans.any");
  return (
    <Form method="get" action={form.action} className="filters" {...filterHandlers(submit)}>
      {form.scope && <input type="hidden" name="scope" value={form.scope} />}
      <div className="filter-row filter-top">
        <fieldset className="filter-products">
          <legend>{t("filter.product")}</legend>
          {form.products.map((p) => (
            <label key={p.value} className="check">
              <input type="checkbox" name="product" value={p.value} defaultChecked={p.checked} />
              {t(`product.${p.value}`)}
            </label>
          ))}
        </fieldset>
        <Input
          className="w-full tab:w-60"
          type="search"
          name="q"
          defaultValue={form.q}
          placeholder={t("plans.search")}
          maxLength={100}
          aria-label={t("plans.search")}
        />
      </div>
      <div className="filter-grid">
        <FilterSelect name="cpu_min" label={t("plans.cpu_min")} options={form.cpu} any={any} />
        <FilterSelect name="ram_min" label={t("plans.ram_min")} options={form.ram} any={any} />
        <FilterSelect name="disk_min" label={t("plans.disk_min")} options={form.disk} any={any} />
        <label className="filter-field">
          <span>{t("plans.price_max")}</span>
          <span className="filter-price">
            <Input
              type="number"
              name="price_max"
              min={1}
              step={1}
              inputMode="numeric"
              defaultValue={form.price_max}
              className="w-full"
            />
            {t("plans.price_unit")}
          </span>
        </label>
        <FilterSelect name="region" label={t("plans.region")} options={form.region} any={any} />
        <FilterSelect name="location" label={t("plans.location")} options={form.location} any={any} />
        <FilterSelect name="period" label={t("plans.period")} options={form.period} any={any} />
      </div>
      <div className="filter-row">
        {form.net.length > 0 && (
          <fieldset className="filter-products">
            <legend>{t("plans.net")}</legend>
            {form.net.map((n) => (
              <label key={n.value} className="check">
                <input type="checkbox" name="net" value={n.value} defaultChecked={n.checked} />
                {n.label}
              </label>
            ))}
          </fieldset>
        )}
        <label className="check">
          <input type="checkbox" name="nvme" value="1" defaultChecked={form.nvme} />
          {t("plans.nvme")}
        </label>
        <label className="check">
          <input type="checkbox" name="nonat" value="1" defaultChecked={form.no_nat} />
          {t("plans.no_nat")}
        </label>
        <label className="filter-sort">
          {t("filter.sort")}
          <NativeSelect name="sort" defaultValue={form.sorts.find((s) => s.selected)?.value}>
            {form.sorts.map((s) => (
              <NativeSelectOption key={s.value} value={s.value}>
                {s.label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </label>
        <Button type="submit" className="filter-submit">
          {t("filter.submit")}
        </Button>
        {form.reset && (
          <Link className="reset" to={form.reset}>
            {t("filter.reset")}
          </Link>
        )}
        <span className="total">{total}</span>
      </div>
    </Form>
  );
}

/** 套餐表：桌面是卡片里的一张表，表头随滚动固定；700 px 以下每行折成一张小卡片（配置格前用 data-l 标出字段名）。
 *  单元格用 data-c 标出是哪一列（样式不靠列序号，加减列不会错位）。机房一格最多两个，其余写"+N"，悬停看全部。 */
function PlanRows({ rows, t }: { rows: PlansPage["rows"]; t: T }) {
  return (
    <div className="panel pv-wrap">
      <table className="pv">
        <thead>
          <tr>
            {["col.vendor", "plan.name", "plan.cpu", "plan.ram", "plan.disk", "plan.bandwidth", "plan.port",
              "plan.location", "plan.price", "plan.cny", "plans.from", "plan.order"].map((key) => (
              <th key={key} scope="col">
                {t(key)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td data-c="vendor">{r.vendor_href ? <Link to={r.vendor_href}>{r.vendor}</Link> : r.vendor}</td>
              <td data-c="name">
                {r.name}
                {r.ip && <small className="ip">{r.ip}</small>}
                {r.net.map((x) => (
                  <small key={x} className="net">
                    {x}
                  </small>
                ))}
              </td>
              <td data-c="cpu" data-l="">
                {r.cpu}
              </td>
              <td data-c="ram" data-l={t("plan.ram")}>
                {r.ram}
              </td>
              <td data-c="disk" data-l={t("plan.disk")}>
                {r.disk}
              </td>
              <td data-c="bw" data-l={t("plan.bandwidth")}>
                {r.bw}
              </td>
              <td data-c="port" data-l={t("plan.port")}>
                {r.port}
              </td>
              <td data-c="loc" data-l={t("plan.location")} title={r.loc_title ?? undefined}>
                {r.loc}
              </td>
              <td data-c="price">
                <b>{r.price}</b>
                {r.code && <CopyCode code={r.code} t={t} />}
              </td>
              <td data-c="cny" data-l={t("plan.cny")}>
                {r.cny}
              </td>
              <td data-c="deal">
                <Link to={r.deal_href} title={r.deal_title}>
                  {t("plans.deal_link")}
                </Link>
              </td>
              <td data-c="order">
                {r.order && (
                  <a href={r.order} rel="nofollow noopener" target="_blank">
                    {t("plans.order_short")}
                  </a>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function PlansView({ loaderData: d }: Route.ComponentProps) {
  const t = useT();
  return (
    <div className="board plans-page">
      <h1 className="page-title">{t("plans.heading")}</h1>
      <ScopeTabs scopes={d.scopes} t={t} />
      {d.presets.length > 0 && (
        <nav className="presets" aria-label={t("plans.presets")}>
          {d.presets.map((x) => (
            <Link key={x.key} to={x.href} aria-current={x.current ? "true" : undefined}>
              {x.label}
            </Link>
          ))}
        </nav>
      )}
      <PlanFilters key={d.seo.canonical} form={d.form} total={d.total} t={t} />
      {d.rows.length > 0 ? <PlanRows rows={d.rows} t={t} /> : <p className="empty">{t("plans.empty")}</p>}
      <Pager pager={d.pager} t={t} />
    </div>
  );
}
