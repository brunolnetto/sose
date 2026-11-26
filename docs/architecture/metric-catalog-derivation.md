# Deriving a business metric catalog from SOSE data (meta_metrics)

- **Status:** Plan
- **Date:** 2026-11-26
- **Related:** ADR-0005 (canonical experiment data product), ADR-0012 (workload
  generation, PR #696), issues #697–#703

## Goal

`meta_metrics` is a business metric catalog with 210 metrics in 15 departments. It
holds entities, semantic attributes and dataset bindings, and it generates dbt
Semantic Layer (MetricFlow) definitions. This plan describes how SOSE can supply
the data to derive **every** metric in it. It states which metrics SOSE can produce,
what each one is missing, and in what order to close the gaps.

The analysis behind it ran all 28 SOSE catalog domains, inventoried their entity
types, states, attributes, events and resources, and compared them with each
metric's entity and formula.

## Two blockers that apply to every metric

1. **No volume and no variation.** Every domain reference runs one configured case,
   with paths chosen by configuration, so a rate over a population is 0 or 1. This
   is deliberate for exact ground truth and frozen evidence. ADR-0012 proposes an
   opt-in, seeded workload mode to fix it.
2. **No monetary or cost model.** Only order-to-cash, cards, insurance,
   record-to-report and credit carry `amount`. No domain has prices, costs, COGS or
   stock value.

## Tiers

Each metric is placed in the first tier that makes it derivable.

1. **Workload.** An existing SOSE domain already has the entities, states and
   timestamps. It needs ADR-0012 workload mode (#697–#702), and its chart needs
   declared transition weights. Some charts exclude every event from sampling
   today, the ITSM `IncidentChart` for example. A metric marked as a proxy measures
   a nearby quantity: ITSM models no customer contacts, so first-contact
   resolution is approximated as "resolved with no escalation and no reopen".
2. **Workload + attributes.** As tier 1, plus modeled attributes on an existing
   domain: prices, costs, customer identity, survey scores, due dates, plan prices.
   These are per-case attribute distributions in the ADR-0012 `WorkloadSpec`.
3. **New domain.** No SOSE domain models the process. It needs a new domain
   (StateCharts, resources, workload) under the usual domain blueprint.
4. **Run metadata.** Derived from SOSE's own execution records (jobs, triggers,
   sinks), not from a business domain.
5. **Exogenous.** Requires data outside an operational simulation: forecasts,
   market totals, external targets. It can only be a configured input, and it is
   never presented as a simulation output.

## Coverage

| Department | Metrics | Workload | Workload+attributes | New domain | Run metadata | Exogenous |
|---|---|---|---|---|---|---|
| Customer | 16 |  | 16 |  |  |  |
| Data | 10 |  |  |  | 10 |  |
| Ecommerce | 15 | 3 | 8 | 4 |  |  |
| Engineering | 12 | 3 |  | 9 |  |  |
| Finance | 27 | 4 | 19 | 4 |  |  |
| Hr | 12 |  |  | 12 |  |  |
| Marketing | 18 |  |  | 17 |  | 1 |
| Operations | 12 | 8 | 3 |  |  | 1 |
| Product | 20 |  |  | 20 |  |  |
| Quality | 8 | 2 | 3 | 3 |  |  |
| Sales | 20 |  |  | 19 |  | 1 |
| Security | 8 |  |  | 8 |  |  |
| Strategy | 8 |  | 5 |  |  | 3 |
| Supply_Chain | 12 | 5 | 5 |  |  | 2 |
| Support | 12 | 9 | 3 |  |  |  |
| **Total** | **210** | **34** | **62** | **96** | **10** | **8** |

Today, the entities, states and timestamps needed already exist for tiers 1 and 4
only: 34 + 10 = 44 metrics, about 21%. Tier 2 (62) has the right domain but lacks
attributes. Even those 44 yield almost no statistically meaningful values until
workload mode exists. Once phases 0–2 are done, 96 metrics (46%) are derivable from
existing domains; with phase 4's run metadata, 106 (50%).

## Path to full derivation

### Phase 0 — Volume (ADR-0012): tier 1, 34 metrics

Deliver #697 (WorkloadSpec and arrivals), #698 (sampled transitions) and #699
(ground truth with tolerance). Then deliver #700 (ITSM, for Support and incident
metrics) and #701 (manufacturing, MRO and warehouse, for Operations, Quality and
Supply chain). #702 guards canonical runs.

### Phase 1 — Export bridge to the catalog's semantic model

A sink that materializes the canonical data product (ADR-0005) for one workload run
as relational datasets, one per SOSE entity type:

- the current entity attributes;
- per-state timestamps pivoted from the transition log, such as `opened_at`,
  `resolved_at`, `po_created_at`, `received_at`;
- resource reservations and release intents, giving occupancy intervals and
  utilization;
- job runs, for tier 4.

Each dataset is registered in the catalog's `dataset` table and bound through
`attribute_binding` to the catalog's semantic attributes. Examples:

| Catalog entity | SOSE source |
|---|---|
| Ticket | `itsm_incident` (`created_at` = opened, `first_response_at` = assigned, `resolved_at` = resolved) |
| Incident | `itsm_incident` |
| Operation | `manufacturing_operation`, MRO work order (`job_shop` is a canonical concurrency case, also registered in the catalog) |
| Production Unit | `production_order` |
| Purchase Order | `p2p.purchase_order` (`po_created_at`, `received_at` from receipt) |
| Order | `order_to_cash.sales_order`, `warehouse_fulfillment_order` |
| Invoice | `sales_order` at `invoiced`, `receivable` |
| Inventory Position | `warehouse_inventory_lot` |
| Subscription | `saas_subscription` |
| Pipeline Run | SOSE job runs |

The catalog's existing dbt generator then produces the metrics unchanged. Output
semantics stay owned by SOSE (ADR-0005), and the catalog only binds to them.

### Phase 2 — Attributes: tier 2, 62 metrics

These are per-domain workload attribute distributions:
- unit prices and costs (O2C, P2P, warehouse, manufacturing);
- customer identity (ITSM, O2C);
- survey scores (ITSM, subscription);
- due dates or SLAs: supplier due dates (P2P) and per-operation due dates
  (manufacturing, MRO). Today their `due_at` is only the time the scheduler fires
  a command;
- defect counts and measured characteristics with spec limits (manufacturing);
- discounts and returns (O2C). Two domains need extending:

- `subscription_saas` needs plan prices and sampled cancellations and plan changes.
  This covers MRR, ARR, churn and retention.
- `record_to_report` needs cost and cash accounts. This covers margins, cash flow,
  burn and runway.

### Phase 3 — New domains: tier 3, 96 metrics

Eight new domains, each with StateCharts, resources and workload. In descending
order of metric count:

1. product usage (20);
2. sales pipeline (19);
3. marketing funnel (17);
4. workforce and recruiting (12);
5. software delivery (9);
6. security operations (8);
7. storefront (4);
8. audit/CAPA and complaints (3).

The four metrics that combine spend with a lifetime model (CAC, CAC payback, LTV,
LTV/CAC) follow once the sales and marketing domains exist. Each new domain is a
separate design, and the roadmap's rule still applies: new semantics, not
duplicated generic entities.

### Phase 4 — Run metadata: tier 4, 10 metrics

Pipeline duration and success or failure come from SOSE job runs. Dataset quality
metrics (completeness, freshness, duplicates, nulls, validity, contracts,
incidents) can be computed on SOSE's persisted records and sinks. However, these
records are validated on write, so the values are close to constant by
construction. They prove the plumbing works; they say little about data quality.

### Exogenous: tier 5, 8 metrics

Forecast accuracy and bias, market share, share of wallet, brand search share and
KPI attainment compare against data SOSE does not simulate. They become derivable
only when that data is supplied as configured exogenous input. The catalog must
mark them as input-dependent.

## Validation

- Every derived metric is tagged with its ADR-0012 ground-truth eligibility. Where
  a mechanistic expectation exists (offered load, branch frequency, arrival count),
  the dbt value is compared with it within tolerance.
- Invariants (conservation, precedence) are checked on the exported datasets.
- The export is reproducible: the same seed produces the same datasets and the same
  metric values.

## Metric-by-metric mapping

The Tier column uses the numbers defined above.

#### Customer

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `customer.churn_rate` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.churned_mrr` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.contraction_mrr` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.csat` | 2 | modeled survey/health attributes |
| `customer.customer_concentration` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.customer_effort_score` | 2 | modeled survey/health attributes |
| `customer.customer_expansion_rate` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.customer_health_score` | 2 | modeled survey/health attributes |
| `customer.expansion_mrr` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.gross_revenue_retention` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.logo_retention_rate` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.nps` | 2 | modeled survey/health attributes |
| `customer.reactivation_mrr` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.renewal_rate` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.revenue_churn_rate` | 2 | subscription_saas + prices, cancellations, plan changes |
| `customer.time_to_value` | 2 | subscription_saas + prices, cancellations, plan changes |

#### Data

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `data.completeness` | 4 | SOSE persisted records and sinks (near-constant by construction: validated records) |
| `data.data_contract_compliance` | 4 | SOSE persisted records and sinks (near-constant by construction: validated records) |
| `data.data_incident_rate` | 4 | SOSE persisted records and sinks (near-constant by construction: validated records) |
| `data.duplicate_rate` | 4 | SOSE persisted records and sinks (near-constant by construction: validated records) |
| `data.freshness` | 4 | SOSE persisted records and sinks (near-constant by construction: validated records) |
| `data.null_rate` | 4 | SOSE persisted records and sinks (near-constant by construction: validated records) |
| `data.pipeline_duration` | 4 | SOSE job runs (SimulationJobState, completed triggers) |
| `data.pipeline_failure_rate` | 4 | SOSE job runs (SimulationJobState, completed triggers) |
| `data.pipeline_success_rate` | 4 | SOSE job runs (SimulationJobState, completed triggers) |
| `data.validity_rate` | 4 | SOSE persisted records and sinks (near-constant by construction: validated records) |

#### Ecommerce

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `ecommerce.average_order_value` | 1 | order_to_cash sales orders |
| `ecommerce.cart_abandonment_rate` | 3 | new storefront domain (session, cart, checkout) |
| `ecommerce.cart_add_rate` | 3 | new storefront domain (session, cart, checkout) |
| `ecommerce.checkout_completion_rate` | 3 | new storefront domain (session, cart, checkout) |
| `ecommerce.conversion_rate` | 3 | new storefront domain (session, cart, checkout) |
| `ecommerce.customer_lifetime_orders` | 2 | order_to_cash + customer id, items, discounts, refunds, returns |
| `ecommerce.discount_rate` | 2 | order_to_cash + customer id, items, discounts, refunds, returns |
| `ecommerce.gross_merchandise_value` | 1 | order_to_cash sales orders |
| `ecommerce.items_per_order` | 2 | order_to_cash + customer id, items, discounts, refunds, returns |
| `ecommerce.net_merchandise_value` | 2 | order_to_cash + discounts and returns |
| `ecommerce.orders` | 1 | order_to_cash sales orders |
| `ecommerce.purchasers` | 2 | order_to_cash + customer id, items, discounts, refunds, returns |
| `ecommerce.refund_rate` | 2 | order_to_cash + customer id, items, discounts, refunds, returns |
| `ecommerce.repeat_purchase_rate` | 2 | order_to_cash + customer id, items, discounts, refunds, returns |
| `ecommerce.return_rate` | 2 | order_to_cash + customer id, items, discounts, refunds, returns |

#### Engineering

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `engineering.change_fail_rate` | 3 | new software-delivery domain (deployments, releases, requests) |
| `engineering.change_lead_time` | 3 | new software-delivery domain (deployments, releases, requests) |
| `engineering.change_volume` | 3 | new software-delivery domain (deployments, releases, requests) |
| `engineering.deployment_frequency` | 3 | new software-delivery domain (deployments, releases, requests) |
| `engineering.deployment_rework_rate` | 3 | new software-delivery domain (deployments, releases, requests) |
| `engineering.error_rate` | 3 | new software-delivery domain (deployments, releases, requests) |
| `engineering.failed_deployment_recovery_time` | 3 | new software-delivery domain (deployments, releases, requests) |
| `engineering.incident_count` | 1 | itsm incidents |
| `engineering.incident_rate` | 1 | itsm incidents |
| `engineering.mean_time_to_recover` | 1 | itsm incidents |
| `engineering.release_success_rate` | 3 | new software-delivery domain (deployments, releases, requests) |
| `engineering.service_availability` | 3 | new software-delivery domain (deployments, releases, requests) |

#### Finance

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `finance.active_paying_accounts` | 2 | subscription_saas + plan prices (MRR) |
| `finance.active_paying_customers` | 2 | subscription_saas + plan prices (MRR) |
| `finance.arpa` | 2 | subscription_saas + plan prices (MRR) |
| `finance.arpu` | 2 | subscription_saas + plan prices (MRR) |
| `finance.arr` | 2 | subscription_saas + plan prices (MRR) |
| `finance.burn_rate` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.cac` | 3 | needs sales/marketing spend (new domains) + LTV model |
| `finance.cac_payback_months` | 3 | needs sales/marketing spend (new domains) + LTV model |
| `finance.cash_balance` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.ebitda` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.ebitda_margin` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.free_cash_flow` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.gross_margin` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.gross_profit` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.gross_revenue` | 1 | order_to_cash invoiced amounts |
| `finance.ltv` | 3 | needs sales/marketing spend (new domains) + LTV model |
| `finance.ltv_cac_ratio` | 3 | needs sales/marketing spend (new domains) + LTV model |
| `finance.mrr` | 2 | subscription_saas + plan prices (MRR) |
| `finance.mrr_growth_rate` | 2 | subscription_saas + plan prices (MRR) |
| `finance.net_margin` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.net_revenue` | 1 | order_to_cash invoiced amounts |
| `finance.net_revenue_retention` | 2 | subscription_saas + plan prices (MRR) |
| `finance.operating_cash_flow` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.operating_margin` | 2 | record_to_report ledger + cost and cash accounts |
| `finance.revenue` | 1 | order_to_cash invoiced amounts |
| `finance.revenue_growth_rate` | 1 | order_to_cash invoiced amounts |
| `finance.runway` | 2 | record_to_report ledger + cost and cash accounts |

#### Hr

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `hr.absenteeism_rate` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.cost_per_hire` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.employee_engagement_score` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.headcount` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.headcount_growth_rate` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.involuntary_turnover_rate` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.offer_acceptance_rate` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.retention_rate` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.revenue_per_employee` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.time_to_fill` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.time_to_hire` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |
| `hr.voluntary_turnover_rate` | 3 | new workforce/recruiting domain (job, offer, hire, employee) |

#### Marketing

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `marketing.brand_search_share` | 5 | exogenous search-market data |
| `marketing.campaign_roi` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.cost_per_lead` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.cost_per_mql` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.cost_per_sql` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.customer_acquisition_growth` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.email_click_rate` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.email_open_rate` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.leads_generated` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.marketing_influenced_revenue` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.marketing_sourced_revenue` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.mql_to_sql_rate` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.mql_volume` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.paid_media_roas` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.sql_volume` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.unsubscribe_rate` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.website_conversion_rate` | 3 | new marketing-funnel domain (campaign, email, session, lead) |
| `marketing.website_sessions` | 3 | new marketing-funnel domain (campaign, email, session, lead) |

#### Operations

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `operations.capacity_utilization` | 1 | manufacturing/mro operations and resources (job_shop canonical case) |
| `operations.cost_per_unit` | 2 | manufacturing + operating cost |
| `operations.cycle_time` | 1 | manufacturing/mro operations and resources (job_shop canonical case) |
| `operations.defect_rate` | 1 | manufacturing/mro operations and resources (job_shop canonical case) |
| `operations.downtime_rate` | 1 | manufacturing/mro operations and resources (job_shop canonical case) |
| `operations.first_pass_yield` | 1 | manufacturing/mro operations and resources (job_shop canonical case) |
| `operations.forecast_accuracy` | 5 | exogenous forecast input |
| `operations.on_time_rate` | 2 | manufacturing/mro + per-operation due date or SLA |
| `operations.productivity` | 1 | manufacturing/mro operations and resources (job_shop canonical case) |
| `operations.rework_rate` | 1 | manufacturing/mro operations and resources (job_shop canonical case) |
| `operations.sla_attainment` | 2 | manufacturing/mro + per-operation due date or SLA |
| `operations.throughput` | 1 | manufacturing/mro operations and resources (job_shop canonical case) |

#### Product

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `product.activation_rate` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.avg_session_duration` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.dau` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.dau_mau_ratio` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.feature_adoption_rate` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.feature_usage_frequency` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.free_to_paid_rate` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.mau` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.north_star_value` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.onboarding_completion_rate` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.product_revenue_per_user` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.retention_d1` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.retention_d30` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.retention_d7` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.session_frequency` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.signup_conversion_rate` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.time_to_activation` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.trial_to_paid_rate` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.user_growth_rate` | 3 | new product-usage domain (user, session, feature, trial) |
| `product.wau` | 3 | new product-usage domain (user, session, feature, trial) |

#### Quality

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `quality.audit_nonconformity_rate` | 3 | new audit/CAPA domain |
| `quality.corrective_action_on_time_rate` | 3 | new audit/CAPA domain |
| `quality.cost_of_poor_quality` | 2 | manufacturing + failure/rework costs |
| `quality.customer_complaint_rate` | 3 | new complaint intake (or itsm extension) |
| `quality.defect_density` | 2 | manufacturing + defect counts per unit |
| `quality.first_time_quality_rate` | 1 | manufacturing inspection/quality_hold/rework, yield |
| `quality.process_capability_index` | 2 | manufacturing + measured characteristic with spec limits |
| `quality.scrap_rate` | 1 | manufacturing inspection/quality_hold/rework, yield |

#### Sales

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `sales.average_days_to_close_won` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.average_deal_size` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.average_revenue_per_sales_rep` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.bookings` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.discount_rate` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.expansion_revenue` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.forecast_accuracy` | 5 | exogenous forecast input |
| `sales.lead_to_opportunity_rate` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.lost_rate` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.new_logo_revenue` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.opportunities_created` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.opportunity_to_customer_rate` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.pipeline_coverage` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.pipeline_value` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.quota_attainment` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.sales_cycle_length` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.sales_target` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.sales_velocity` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.weighted_pipeline` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |
| `sales.win_rate` | 3 | new sales-pipeline domain (lead, opportunity, contract, quota) |

#### Security

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `security.critical_vulnerability_count` | 3 | new security-operations domain (detect, contain, remediate) |
| `security.mean_time_to_contain` | 3 | new security-operations domain (detect, contain, remediate) |
| `security.mean_time_to_detect` | 3 | new security-operations domain (detect, contain, remediate) |
| `security.mean_time_to_remediate` | 3 | new security-operations domain (detect, contain, remediate) |
| `security.phishing_failure_rate` | 3 | new security-operations domain (detect, contain, remediate) |
| `security.security_control_compliance` | 3 | new security-operations domain (detect, contain, remediate) |
| `security.security_incident_count` | 3 | new security-operations domain (detect, contain, remediate) |
| `security.vulnerability_remediation_sla_rate` | 3 | new security-operations domain (detect, contain, remediate) |

#### Strategy

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `strategy.customer_count` | 2 | order_to_cash/subscription_saas customers |
| `strategy.customer_growth_rate` | 2 | order_to_cash/subscription_saas customers |
| `strategy.market_share` | 5 | exogenous market data |
| `strategy.new_customer_count` | 2 | order_to_cash/subscription_saas customers |
| `strategy.profitability_per_customer` | 2 | order_to_cash/subscription_saas customers |
| `strategy.revenue_per_customer` | 2 | order_to_cash/subscription_saas customers |
| `strategy.share_of_wallet` | 5 | exogenous market data |
| `strategy.strategic_kpi_attainment` | 5 | exogenous KPI targets |

#### Supply_Chain

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `supply_chain.days_inventory_outstanding` | 2 | warehouse + unit cost / COGS |
| `supply_chain.fill_rate` | 1 | warehouse_fulfillment/order_to_cash orders |
| `supply_chain.forecast_accuracy` | 5 | exogenous forecast input |
| `supply_chain.forecast_bias` | 5 | exogenous forecast input |
| `supply_chain.inventory_turnover` | 2 | warehouse + unit cost / COGS |
| `supply_chain.inventory_units` | 1 | warehouse inventory lots |
| `supply_chain.inventory_value` | 2 | warehouse + unit cost / COGS |
| `supply_chain.order_fulfillment_cycle_time` | 1 | warehouse_fulfillment/order_to_cash orders |
| `supply_chain.procurement_savings` | 2 | p2p + baseline and actual prices |
| `supply_chain.purchase_order_cycle_time` | 1 | p2p purchase orders and receipts |
| `supply_chain.stockout_rate` | 1 | p2p material demand backordered state |
| `supply_chain.supplier_on_time_rate` | 2 | p2p + supplier due dates |

#### Support

| Metric | Tier | SOSE source or gap |
|---|---|---|
| `support.agent_utilization` | 1 | itsm incidents, transitions, agent resources |
| `support.backlog` | 1 | itsm incidents, transitions, agent resources |
| `support.cost_per_ticket` | 2 | itsm + support cost per agent-hour |
| `support.csat` | 2 | itsm + modeled survey score |
| `support.escalation_rate` | 1 | itsm incidents, transitions, agent resources |
| `support.first_contact_resolution_rate` | 1 | itsm proxy: resolved with no escalation and no reopen (no contacts modeled) |
| `support.first_response_time` | 1 | itsm incidents, transitions, agent resources |
| `support.resolution_time` | 1 | itsm incidents, transitions, agent resources |
| `support.sla_breach_rate` | 1 | itsm incidents, transitions, agent resources |
| `support.ticket_reopen_rate` | 1 | itsm incidents, transitions, agent resources |
| `support.ticket_volume` | 1 | itsm incidents, transitions, agent resources |
| `support.tickets_per_customer` | 2 | itsm + per-case customer id |
