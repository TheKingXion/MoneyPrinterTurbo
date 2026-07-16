from datetime import datetime
import time

import streamlit as st

from app.services import api_usage


def _number(value) -> str:
    return f"{int(value or 0):,}".replace(",", ".")


def render(tr) -> None:
    with st.expander(tr("API usage meter"), expanded=False):
        st.caption(
            tr(
                "Counts model API requests and tokens without storing prompts, responses, or API keys."
            )
        )
        period_label = st.selectbox(
            tr("Period"),
            (tr("Last 7 days"), tr("Last 30 days"), tr("Last 90 days"), tr("All time")),
            index=1,
            key="api_usage_period",
        )
        days = {
            tr("Last 7 days"): 7,
            tr("Last 30 days"): 30,
            tr("Last 90 days"): 90,
            tr("All time"): None,
        }[period_label]
        since = None if days is None else time.time() - days * 86400

        store = api_usage.get_api_usage_store()
        available = store.filters(since=since)
        filters = st.columns(3)
        providers = filters[0].multiselect(
            tr("Providers"), available["provider"], key="api_usage_providers"
        )
        models = filters[1].multiselect(
            tr("Models"), available["model"], key="api_usage_models"
        )
        categories = filters[2].multiselect(
            tr("Categories"), available["category"], key="api_usage_categories"
        )
        report = store.report(
            since=since, providers=providers, models=models, categories=categories
        )
        totals = report["totals"]
        metrics = st.columns(6)
        metrics[0].metric(tr("API requests"), _number(totals["requests"]))
        metrics[1].metric(tr("Input tokens"), _number(totals["input_tokens"]))
        metrics[2].metric(tr("Output tokens"), _number(totals["output_tokens"]))
        metrics[3].metric(tr("Total tokens"), _number(totals["total_tokens"]))
        metrics[4].metric(tr("Estimated requests"), _number(totals["estimated_requests"]))
        metrics[5].metric(tr("Failed requests"), _number(totals["failed_requests"]))

        if not totals["requests"]:
            st.info(tr("No API usage has been recorded for these filters yet."))
            return

        st.caption(tr("Usage by provider and model"))
        st.dataframe(report["by_model"], hide_index=True, width="stretch")
        st.caption(tr("What the tokens were used for"))
        st.dataframe(report["by_category"], hide_index=True, width="stretch")
        st.caption(tr("Recent API activity"))
        recent = []
        for row in report["recent"]:
            item = dict(row)
            item["created_at"] = datetime.fromtimestamp(item["created_at"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            item["estimated"] = tr("Estimated") if item["estimated"] else tr("Reported")
            recent.append(item)
        st.dataframe(recent, hide_index=True, width="stretch")
