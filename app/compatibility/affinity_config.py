from urllib.parse import urlsplit

def legacy_affinity(settings, channel, profile, header):
    if header is None and channel == "aihubmix": header = "X-Aihubmix-Session-Id"
    hosts = profile.get("affinity_hosts")
    if hosts is None and channel == "aihubmix":
        hosts = [urlsplit(url).hostname for url in (settings.aihubmix_openai_base_url, settings.aihubmix_claude_base_url,
            settings.aihubmix_gemini_base_url, settings.aihubmix_gemini_sdk_base_url) if url]
    return header, hosts
