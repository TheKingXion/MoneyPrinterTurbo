import dataclasses

import streamlit as st

from app.services import performance


def _gib(value: int | float | None) -> str:
    return f"{float(value or 0) / performance.GIB:.1f} GB"


def render(tr) -> None:
    with st.expander(tr("Performance"), expanded=False):
        profile = performance.get_runtime_profile()
        hardware = performance.detect_hardware()
        gpu = hardware.gpus[0] if hardware.gpus else None

        metrics = st.columns(6)
        metrics[0].metric("CPU", f"{hardware.cpu_physical}C / {hardware.cpu_logical}T")
        metrics[1].metric("RAM libre", _gib(hardware.ram_available))
        metrics[2].metric("GPU", gpu.name if gpu else "No detectada")
        metrics[3].metric("VRAM libre", _gib(gpu.vram_free if gpu else 0))
        metrics[4].metric("Disco libre", _gib(hardware.disk_free))
        metrics[5].metric("Encoder", profile.h264_codec)

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
            live[0].metric("CPU proceso", f"{float(resource.get('cpu_percent') or 0):.0f}%")
            live[1].metric("RAM proceso", _gib(resource.get("rss_bytes")))
            live[2].metric("GPU", f"{float(resource.get('gpu_percent') or 0):.0f}%")
            live[3].metric("VRAM usada", _gib(resource.get("gpu_memory_used")))

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
