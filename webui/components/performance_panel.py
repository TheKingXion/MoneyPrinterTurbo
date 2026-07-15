import dataclasses

import streamlit as st

from app.services import performance


def _gib(value: int | float | None) -> str:
    return "N/A" if value is None else f"{float(value) / performance.GIB:.1f} GB"


def _percent(value: int | float | None) -> str:
    return "N/A" if value is None else f"{float(value):.0f}%"


def _temperature(value: int | float | None) -> str:
    return "N/A" if value is None else f"{float(value):.0f} °C"


def render(tr) -> None:
    with st.expander(tr("Performance"), expanded=False):
        profile = performance.get_runtime_profile()
        hardware = performance.detect_hardware()

        metrics = st.columns(6)
        metrics[0].metric(
            "CPU",
            tr("CPU core summary").format(
                physical=hardware.cpu_physical, logical=hardware.cpu_logical
            ),
        )
        metrics[1].metric(tr("Free RAM"), _gib(hardware.ram_available))
        metrics[2].metric(tr("CPU temperature"), _temperature(hardware.cpu_temperature_c))
        metrics[3].metric(
            "GPU", str(len(hardware.gpus)) if hardware.gpus else tr("Not detected")
        )
        metrics[4].metric(tr("Free disk"), _gib(hardware.disk_free))
        metrics[5].metric(tr("Encoder"), profile.h264_codec)

        if hardware.cpu_name:
            st.caption(f"CPU: {hardware.cpu_name}")
        if hardware.cpu_temperature_source:
            st.caption(f"{tr('CPU sensor')}: {hardware.cpu_temperature_source}")
        elif hardware.cpu_temperature_c is None:
            st.caption(tr("CPU sensor unavailable"))

        if hardware.gpus:
            st.dataframe(
                [
                    {
                        tr("Vendor"): gpu.vendor.upper(),
                        "GPU": gpu.name,
                        tr("Total VRAM"): _gib(gpu.vram_total),
                        tr("Free VRAM"): _gib(gpu.vram_free),
                        tr("Usage"): _percent(gpu.utilization_percent),
                        tr("Temperature"): _temperature(gpu.temperature_c),
                        tr("Driver"): gpu.driver_version or "N/A",
                        tr("Source"): gpu.metrics_source or tr("Inventory only"),
                    }
                    for gpu in hardware.gpus
                ],
                hide_index=True,
                use_container_width=True,
            )

        st.caption(
            f"Automático adaptativo · {profile.render_slots} render simultáneo(s) · "
            f"{profile.ffmpeg_threads} hilos FFmpeg · {profile.network_slots} trabajos de red"
        )
        if profile.disk_low:
            st.warning("El espacio libre es bajo; limpia la caché antes de ejecutar lotes grandes.")

        if st.button(tr("Redetect Hardware"), key="performance_reprobe"):
            with st.spinner("Probando codificadores disponibles..."):
                performance.get_runtime_profile(force=True)
            st.rerun()

        telemetry = performance.get_telemetry()
        summary = telemetry.summary()
        if summary.get("runs"):
            st.caption(
                f"Promedio por tarea: {float(summary.get('average_seconds') or 0):.1f} s · "
                f"Rendimiento estimado: {float(summary.get('tasks_per_hour') or 0):.1f} tareas/h · "
                f"Próxima tarea: ~{float(summary.get('estimated_task_seconds') or 0):.1f} s"
            )
        resource = telemetry.latest_resource_sample()
        if resource:
            live = st.columns(4)
            live[0].metric(tr("Process CPU"), _percent(resource.get("cpu_percent")))
            live[1].metric(tr("Process RAM"), _gib(resource.get("rss_bytes")))
            live[2].metric("GPU", _percent(resource.get("gpu_percent")))
            live[3].metric(tr("Used VRAM"), _gib(resource.get("gpu_memory_used")))

        stages = telemetry.aggregate_stage_timings()
        if stages:
            st.dataframe(
                [
                    {
                        "Etapa": row["name"],
                        "Ejecuciones": row["runs"],
                        "Promedio (s)": round(float(row["average_seconds"] or 0), 2),
                        "Máximo (s)": round(float(row["max_seconds"] or 0), 2),
                        "Fallos": row["failures"],
                    }
                    for row in stages
                ],
                hide_index=True,
                use_container_width=True,
            )
