import streamlit as st

from app.services import llm


def initialize_state() -> None:
    st.session_state.setdefault("social_title", "")
    st.session_state.setdefault("social_caption", "")
    st.session_state.setdefault("social_hashtags", "")
    st.session_state.setdefault("social_platform", llm.DEFAULT_SOCIAL_PLATFORM)


def generate(subject: str, script: str, language: str) -> None:
    metadata = llm.generate_social_metadata(
        video_subject=subject,
        video_script=script,
        language=language,
        platform=st.session_state["social_platform"],
    )
    st.session_state["social_title"] = metadata["title"]
    st.session_state["social_caption"] = metadata["caption"]
    st.session_state["social_hashtags"] = " ".join(metadata["hashtags"])


def render(tr, subject: str, script: str, language: str) -> None:
    initialize_state()
    with st.expander(tr("Publishing Description"), expanded=False):
        platforms = list(llm.SOCIAL_PLATFORMS)
        current = st.session_state["social_platform"]
        st.session_state["social_platform"] = st.selectbox(
            tr("Publishing Platform"),
            platforms,
            index=platforms.index(current) if current in platforms else 0,
            format_func=lambda value: llm.SOCIAL_PLATFORM_LABELS[value],
            key="social_metadata_platform",
        )
        if st.button(
            tr("Generate Publishing Description"),
            key="social_metadata_generate",
            use_container_width=True,
        ):
            if not subject and not script:
                st.error(tr("Video Script and Subject Cannot Both Be Empty"))
            else:
                with st.spinner(tr("Generating Publishing Description")):
                    generate(subject, script, language)
        st.text_input(tr("Publishing Title"), key="social_title")
        st.text_area(
            tr("Publishing Description Text"), key="social_caption", height=140
        )
        st.text_input(tr("Publishing Hashtags"), key="social_hashtags")
