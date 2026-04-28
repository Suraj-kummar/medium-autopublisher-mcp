import asyncio
import uuid
import structlog
from scheduler.job_manager import JobManager
from pipeline.orchestrator import run

# Reduce logging noise for the test
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(20)  # INFO level
)

async def test_pipeline():
    print("=" * 60)
    print("  MEDIUM AUTOPUBLISHER — LIVE PIPELINE TEST")
    print("=" * 60)
    print("This will take ~60-120 seconds...")
    print()
    print("Stage 1 ► Researching topic via Tavily...")
    print("Stage 2 ► Writing article via Google Gemini...")
    print("Stage 3 ► Validating content quality...")
    print("Stage 4 ► Generating header image via Pollinations.ai...")
    print("Stage 5 ► Saving draft locally...")
    print()

    jm = JobManager()

    topic = "The Future of AI Agents in Software Engineering"
    from scheduler.fingerprint import make_fingerprint
    fingerprint = make_fingerprint(topic, None)
    job_id = f"test-{uuid.uuid4().hex[:8]}"
    jm.create_job(job_id, fingerprint, topic, ["AI", "Software", "Tech"], "Professional and authoritative", None)

    try:
        draft_path = await run(
            job_id=job_id,
            topic=topic,
            tags=["AI", "Software", "Tech"],
            style_hint="Professional and authoritative",
            job_manager=jm,
        )
        print()
        print("=" * 60)
        print("  ✅ SUCCESS! Article generated and saved.")
        print(f"  📄 Draft saved to: {draft_path}")
        print("=" * 60)
    except Exception as e:
        print()
        print("=" * 60)
        print(f"  ❌ FAILED: {e}")
        print("=" * 60)
        raise

if __name__ == "__main__":
    asyncio.run(test_pipeline())
