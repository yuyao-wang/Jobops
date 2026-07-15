#!/usr/bin/env python3
"""
MR.Jobs — AI-Powered Job Intelligence
======================================

Uses Claude Code CLI as the AI brain + Playwright for browser automation.

Usage:
    # Discover & review matches (no applications sent)
    python main.py discover

    # Dry run — fill forms but don't submit
    python main.py apply --dry-run

    # Actually apply (use with caution!)
    python main.py apply

    # Apply to a single URL
    python main.py single https://boards.greenhouse.io/company/jobs/12345

    # View stats
    python main.py stats
"""

import asyncio
import argparse
import copy
import random
import sys
import yaml
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

from utils.brain import ClaudeBrain
from utils.discovery import discover_all_jobs
from utils.tracker import (
    is_already_seen, log_discovered, log_matched,
    log_applied, log_skipped, get_today_count, print_stats,
    reset_unscored, delete_all, get_unscored_jobs
)
from adapters.stagehand_adapter import apply_smart
from utils.browser_session import launch_browser_session
from utils.csv_apply import load_csv_queue, update_csv_application


def load_profile(path: str = "profile.yaml") -> dict:
    """Load and validate profile config."""
    p = Path(path)
    if not p.exists():
        print(f"❌ Profile not found: {path}")
        print(f"   Copy profile.yaml.example to profile.yaml and fill it out.")
        sys.exit(1)

    with open(p) as f:
        profile = yaml.safe_load(f)

    # Validate required fields
    personal = profile.get("personal", {})
    required = ["first_name", "last_name", "email"]
    missing = [f for f in required if not personal.get(f)]
    if missing:
        print(f"❌ Missing required fields in profile.yaml: {', '.join(missing)}")
        sys.exit(1)

    # Validate resume exists
    resume = profile.get("resume_path", "")
    if resume and not Path(resume).exists():
        print(f"⚠ Resume not found at: {resume}")
        print(f"  Applications requiring resume upload will fail.")

    return profile


async def cmd_discover(profile: dict):
    """Discover jobs and score them — no applications sent."""
    brain = ClaudeBrain(verbose=True, profile=profile)
    from utils.resume_parser import extract_resume_text
    resume_text = extract_resume_text(profile.get("resume_path", ""))

    print("\n🔍 Discovering jobs from configured boards...\n")
    jobs = await discover_all_jobs(profile)

    if not jobs:
        print("\n😕 No matching jobs found. Try:")
        print("   - Adding more companies to target_boards in profile.yaml")
        print("   - Broadening role keywords in preferences.roles")
        return

    min_score = profile["preferences"].get("min_match_score", 65)
    matches = []

    print(f"\n🧠 Scoring {len(jobs)} jobs with Claude (min score: {min_score})...\n")

    for i, job in enumerate(jobs):
        # Skip already-seen jobs
        if is_already_seen(job.id):
            print(f"  [{i+1}/{len(jobs)}] ⏭ Already seen: {job.title} @ {job.company}")
            continue

        log_discovered(job)

        print(f"  [{i+1}/{len(jobs)}] 🔍 {job.title} @ {job.company} ({job.location})")

        try:
            result = brain.match_job(job.description, profile, resume_text=resume_text)
            score = result.get("score", 0)
            should_apply = result.get("apply", False)
            reasoning = result.get("reasoning", "")
            cover_letter = result.get("cover_letter", "")

            log_matched(job.id, score, reasoning, cover_letter)

            emoji = "✅" if should_apply else "❌"
            print(f"           {emoji} Score: {score} — {reasoning}")

            if should_apply and score >= min_score:
                matches.append((job, result))
            else:
                log_skipped(job.id, f"Score {score} < {min_score}: {reasoning}")

        except Exception as e:
            print(f"           ⚠ Scoring failed: {e}")

    print(f"\n{'='*60}")
    print(f"📊 Results: {len(matches)} jobs above threshold out of {len(jobs)} scanned")
    print(f"{'='*60}")
    for job, result in matches:
        print(f"\n  🎯 {job.title} @ {job.company}")
        print(f"     Location: {job.location}")
        print(f"     Score: {result['score']}")
        print(f"     URL: {job.apply_url}")
        if result.get("skill_overlap"):
            print(f"     Matching: {', '.join(result['skill_overlap'][:5])}")
        if result.get("red_flags"):
            print(f"     Flags: {', '.join(result['red_flags'])}")

    print_stats()


async def cmd_apply(profile: dict, dry_run: bool = True):
    """Discover, score, and apply to matching jobs."""
    brain = ClaudeBrain(verbose=True, profile=profile)
    from utils.resume_parser import extract_resume_text
    resume_text = extract_resume_text(profile.get("resume_path", ""))
    rate_limits = profile.get("rate_limits", {})
    max_per_day = rate_limits.get("max_applications_per_day", 25)
    min_delay = rate_limits.get("min_delay_seconds", 60)
    max_delay = rate_limits.get("max_delay_seconds", 180)

    today_count = get_today_count()
    if today_count >= max_per_day:
        print(f"🛑 Daily limit reached ({today_count}/{max_per_day}). Try again tomorrow.")
        return

    # Discover
    print("\n🔍 Discovering jobs...\n")
    jobs = await discover_all_jobs(profile)
    if not jobs:
        print("No matching jobs found.")
        return

    # Score
    min_score = profile["preferences"].get("min_match_score", 65)
    matches = []

    print(f"\n🧠 Scoring {len(jobs)} jobs...\n")
    for job in jobs:
        if is_already_seen(job.id):
            continue
        log_discovered(job)
        try:
            result = brain.match_job(job.description, profile, resume_text=resume_text)
            score = result.get("score", 0)
            log_matched(job.id, score, result.get("reasoning", ""), result.get("cover_letter", ""))
            if result.get("apply") and score >= min_score:
                matches.append((job, result))
                print(f"  ✅ {score}: {job.title} @ {job.company}")
            else:
                log_skipped(job.id, result.get("reasoning", "Low score"))
                print(f"  ❌ {score}: {job.title} @ {job.company}")
        except Exception as e:
            print(f"  ⚠ {job.title} @ {job.company}: {e}")

    if not matches:
        print("\nNo jobs above the match threshold.")
        print_stats()
        return

    # Apply
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n{'='*60}")
    print(f"🚀 Applying to {len(matches)} jobs [{mode}]")
    print(f"{'='*60}\n")

    async with async_playwright() as p:
        session = await launch_browser_session(p, profile, headless=False)
        page = session.page

        for i, (job, result) in enumerate(matches):
            if get_today_count() >= max_per_day:
                print(f"\n🛑 Daily limit reached ({max_per_day}). Stopping.")
                break

            print(f"\n{'─'*50}")
            print(f"[{i+1}/{len(matches)}] {job.title} @ {job.company}")
            print(f"  URL: {job.apply_url}")
            print(f"  Score: {result['score']} — {result.get('reasoning', '')}")

            try:
                cover_letter = result.get("cover_letter", "")

                success = await apply_smart(
                    page, job.apply_url, profile, brain,
                    cover_letter=cover_letter, dry_run=dry_run,
                    platform=job.platform,
                    company=job.company, title=job.title,
                    description=getattr(job, 'description', ''),
                )

                if not dry_run:
                    log_applied(job.id, success)

            except Exception as e:
                print(f"  ❌ Application failed: {e}")
                if not dry_run:
                    log_applied(job.id, False)

            # Rate limiting
            if i < len(matches) - 1:
                delay = random.randint(min_delay, max_delay)
                print(f"  ⏳ Waiting {delay}s before next application...")
                await asyncio.sleep(delay)

        await session.close()

    print_stats()


async def cmd_single(
    profile: dict,
    url: str,
    dry_run: bool = True,
    company: str = "",
    title: str = "",
):
    """Apply to a single job URL."""
    brain = ClaudeBrain(verbose=True, profile=profile)

    print(f"\n🎯 Single application: {url}")
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"   Mode: {mode}\n")

    async with async_playwright() as p:
        session = await launch_browser_session(p, profile, headless=False)
        page = session.page

        await apply_smart(
            page, url, profile, brain, dry_run=dry_run,
            company=company, title=title,
        )

        if dry_run:
            print("\n💡 Browser staying open for review. Press Ctrl+C to exit.")
            try:
                await asyncio.sleep(300)  # Keep browser open 5 min for review
            except KeyboardInterrupt:
                pass

        await session.close()


def _print_csv_queue(queue, csv_path: str, dry_run: bool) -> None:
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"\n{'='*60}")
    print(f"CSV application queue [{mode}] — {len(queue)} selected")
    print(f"Source: {Path(csv_path).expanduser().resolve()}")
    print(f"{'='*60}")
    for index, application in enumerate(queue, start=1):
        row = application.row
        resume_state = "OK" if application.resume_path.is_file() else "MISSING"
        print(
            f"  {index:>2}. [{row.get('priority', '')} / {row.get('status', '')}] "
            f"{application.title} @ {application.company}"
        )
        print(f"      Resume: {application.resume_path.name or '(blank)'} [{resume_state}]")
        print(f"      URL: {application.url or '(blank)'}")


async def cmd_apply_csv(
    profile: dict,
    csv_path: str,
    resume_dir: str,
    priorities: str,
    statuses: str,
    limit: int = 0,
    dry_run: bool = True,
    preview: bool = False,
    continue_on_failure: bool = False,
    hold_on_blocker: int = 300,
):
    """Apply only to selected rows from an existing CSV job pool."""
    queue = load_csv_queue(
        csv_path,
        resume_dir,
        priorities=priorities,
        statuses=statuses,
        limit=limit,
    )
    _print_csv_queue(queue, csv_path, dry_run=dry_run)

    if not queue:
        print("\nNo CSV rows matched the requested priority/status filters.")
        return
    if preview:
        print("\nPreview only — no browser opened and the CSV was not changed.")
        return

    rate_limits = profile.get("rate_limits", {})
    max_per_day = int(rate_limits.get("max_applications_per_day", 25))
    min_delay = int(rate_limits.get("min_delay_seconds", 60))
    max_delay = int(rate_limits.get("max_delay_seconds", 180))
    if min_delay > max_delay:
        min_delay, max_delay = max_delay, min_delay

    if not dry_run:
        remaining = max_per_day - get_today_count()
        if remaining <= 0:
            print(f"\nDaily limit reached ({max_per_day}). Nothing was submitted.")
            return
        if len(queue) > remaining:
            print(f"\nDaily limit leaves {remaining} slot(s); trimming this run.")
            queue = queue[:remaining]

    brain = ClaudeBrain(verbose=True, profile=profile)
    completed = 0
    submitted = 0
    needs_user = 0

    async with async_playwright() as playwright:
        session = await launch_browser_session(playwright, profile, headless=False)
        page = session.page
        print(f"Persistent Chromium state: {session.user_data_dir}")

        for index, application in enumerate(queue, start=1):
            print(f"\n{'─'*60}")
            print(f"[{index}/{len(queue)}] {application.title} @ {application.company}")
            print(f"Priority/status: {application.row.get('priority')} / {application.row.get('status')}")

            blocker = ""
            success = False
            if not application.url:
                blocker = "MR.Jobs apply-csv: job_url is blank."
            elif not application.resume_path.is_file():
                blocker = f"MR.Jobs apply-csv: resume file not found: {application.resume_path}"
            else:
                job_profile = copy.deepcopy(profile)
                job_profile["resume_path"] = str(application.resume_path)
                try:
                    success = await apply_smart(
                        page,
                        application.url,
                        job_profile,
                        brain,
                        cover_letter="",
                        dry_run=dry_run,
                        company=application.company,
                        title=application.title,
                        description=application.row.get("notes", ""),
                    )
                except Exception as exc:
                    blocker = f"MR.Jobs apply-csv error: {type(exc).__name__}: {exc}"
                    print(f"  Application error: {exc}")

            completed += 1
            if dry_run:
                if blocker:
                    print(f"  Needs user: {blocker}")
                else:
                    print(f"  Dry-run result: {'form reached/filled' if success else 'not completed'}")
            else:
                confirmed = False
                if success:
                    from adapters.stagehand_adapter import _detect_page_state
                    confirmed = await _detect_page_state(page) == "confirmation"

                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                if success and confirmed:
                    submitted += 1
                    update_csv_application(
                        csv_path,
                        application,
                        {
                            "status": "Submitted",
                            "blocker": "",
                            "skip_reason": "",
                            "next_action": "Monitor email for confirmation and follow-up.",
                        },
                        note=f"{timestamp} MR.Jobs apply-csv: explicit submission confirmation detected.",
                    )
                    print("  CSV updated: Submitted (explicit confirmation detected)")
                else:
                    needs_user += 1
                    blocker = blocker or (
                        application.row.get("blocker", "").strip()
                        or "MR.Jobs live attempt did not reach explicit submission confirmation."
                    )
                    next_action = (
                        application.row.get("next_action", "").strip()
                        or "Resolve the blocker in the open browser, then rerun apply-csv."
                    )
                    update_csv_application(
                        csv_path,
                        application,
                        {
                            "status": "Needs user",
                            "blocker": blocker,
                            "next_action": next_action,
                        },
                        note=f"{timestamp} MR.Jobs apply-csv: stopped without explicit submission confirmation.",
                    )
                    print(f"  Needs user: {blocker}")
                    print("  CSV updated: Needs user")

                    if not continue_on_failure:
                        if hold_on_blocker > 0:
                            print(
                                f"  Browser will stay open for {hold_on_blocker}s for inspection; "
                                "press Ctrl+C to close it sooner."
                            )
                            try:
                                await asyncio.sleep(hold_on_blocker)
                            except KeyboardInterrupt:
                                pass
                        break

            if index < len(queue) and (dry_run or success):
                delay = random.randint(min_delay, max_delay)
                print(f"  Waiting {delay}s before the next CSV row...")
                await asyncio.sleep(delay)

        await session.close()

    print(
        f"\nCSV run complete: {completed} attempted, {submitted} submitted, "
        f"{needs_user} needs user."
    )


async def cmd_workday_session(profile: dict, url: str, browser_name: str, hold: int = 1800):
    """Open a Workday URL for user login in Safari or persistent Chromium."""
    browser_name = browser_name.casefold()
    if browser_name == "safari":
        import subprocess

        subprocess.run(["open", "-a", "Safari", url], check=True)
        print("Workday opened in Safari for user login.")
        print("Note: Safari cookies cannot be imported into Playwright Chromium.")
        return
    if browser_name != "chromium":
        raise ValueError("browser must be safari or chromium")

    async with async_playwright() as playwright:
        session = await launch_browser_session(playwright, profile, headless=False)
        await session.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        print(f"Persistent Workday Chromium session: {session.user_data_dir}")
        print(f"Browser will stay open for {hold}s; finish login, then close it or press Ctrl+C.")
        try:
            await asyncio.sleep(hold)
        except KeyboardInterrupt:
            pass
        await session.close()


def cmd_workday_keychain(profile: dict, url: str, action: str) -> None:
    """Inspect or securely set the tenant Workday credential in macOS Keychain."""
    import getpass
    from utils.keychain import get_workday_credential, save_workday_credential, workday_service

    email = profile.get("personal", {}).get("email", "").strip()
    if action == "status":
        credential = get_workday_credential(url, email)
        state = "stored" if credential else "not stored"
        print(f"Workday Keychain credential: {state}")
        print(f"Service: {workday_service(url)}")
        return

    password = getpass.getpass("Workday password (input hidden): ")
    confirmation = getpass.getpass("Confirm Workday password: ")
    if password != confirmation:
        raise ValueError("passwords do not match")
    service = save_workday_credential(url, email, password)
    print(f"Workday credential saved to macOS Keychain service: {service}")


def cmd_reset():
    """Delete all tracked jobs for a fresh start."""
    count = delete_all()
    print(f"Deleted {count} jobs. Database is clean.")


async def cmd_rescore(profile: dict):
    """Re-score all unscored jobs."""
    import httpx
    import re as _re
    brain = ClaudeBrain(verbose=True, profile=profile)
    from utils.resume_parser import extract_resume_text
    resume_text = extract_resume_text(profile.get("resume_path", ""))
    unscored = get_unscored_jobs()

    if not unscored:
        print("No unscored jobs found.")
        return

    min_score = profile["preferences"].get("min_match_score", 65)
    print(f"\nRe-scoring {len(unscored)} unscored jobs...\n")

    for i, job_row in enumerate(unscored):
        print(f"  [{i+1}/{len(unscored)}] {job_row['title']} @ {job_row['company']}")
        try:
            desc = ""
            if job_row['platform'] == 'greenhouse':
                url = (
                    f"https://boards-api.greenhouse.io/v1/boards/"
                    f"{job_row['company']}/jobs/{job_row['id']}?content=true"
                )
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw = data.get("content", "")
                        desc = _re.sub(r'<[^>]+>', ' ', raw)
                        desc = _re.sub(r'\s+', ' ', desc).strip()[:5000]
            elif job_row['platform'] == 'lever':
                url = (
                    f"https://api.lever.co/v0/postings/"
                    f"{job_row['company']}/{job_row['id']}"
                )
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        desc = data.get("descriptionPlain", "")[:5000]

            if not desc:
                desc = (
                    f"Job: {job_row['title']} at {job_row['company']}. "
                    f"Location: {job_row['location']}"
                )

            result = brain.match_job(desc, profile, resume_text=resume_text)
            score = result.get("score", 0)
            reasoning = result.get("reasoning", "")
            cover_letter = result.get("cover_letter", "")

            log_matched(job_row['id'], score, reasoning, cover_letter)

            emoji = "✅" if score >= min_score else "❌"
            print(f"           {emoji} Score: {score} — {reasoning}")

            if score < min_score:
                log_skipped(job_row['id'], f"Score {score} < {min_score}: {reasoning}")

        except Exception as e:
            print(f"           ⚠ Scoring failed: {e}")

    print_stats()


def main():
    parser = argparse.ArgumentParser(
        description="MR.Jobs — AI-Powered Job Intelligence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py discover                          # Find & score jobs
  python main.py apply --dry-run                   # Fill forms, don't submit
  python main.py apply                             # Actually submit applications
  python main.py single https://boards.greenhouse.io/company/jobs/123
  python main.py single https://jobs.lever.co/company/abc --live
  python main.py apply-csv job_pool.csv --resume-dir resumes/pdf --preview
  python main.py apply-csv job_pool.csv --resume-dir resumes/pdf --live
  python main.py workday-session <url> --browser safari
  python main.py workday-keychain <url> --action status
  python main.py stats                             # View application stats
        """
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # discover
    subparsers.add_parser("discover", help="Discover and score jobs (no applications)")

    # apply
    apply_parser = subparsers.add_parser("apply", help="Discover, score, and apply")
    apply_parser.add_argument("--dry-run", action="store_true", default=True,
                              help="Fill forms but don't submit (default)")
    apply_parser.add_argument("--live", action="store_true",
                              help="Actually submit applications")

    # single
    single_parser = subparsers.add_parser("single", help="Apply to a single URL")
    single_parser.add_argument("url", help="Job posting URL")
    single_parser.add_argument("--live", action="store_true",
                               help="Actually submit (default: dry run)")
    single_parser.add_argument("--company", default="",
                               help="Company name, used to resolve aggregator URLs")
    single_parser.add_argument("--title", default="",
                               help="Job title, used to resolve aggregator URLs")
    single_parser.add_argument("--resume", default="",
                               help="Resume file to use for this application")

    # apply-csv
    csv_parser = subparsers.add_parser(
        "apply-csv",
        help="Apply from an existing CSV queue without discovery or scoring",
    )
    csv_parser.add_argument("csv_path", help="CSV job-pool path")
    csv_parser.add_argument(
        "--resume-dir", required=True,
        help="Directory containing the CSV resume_variant files",
    )
    csv_parser.add_argument(
        "--priorities", default="High,Medium,Low",
        help="Comma-separated priorities in processing order",
    )
    csv_parser.add_argument(
        "--statuses", default="Needs user,Pending",
        help="Comma-separated statuses in processing order",
    )
    csv_parser.add_argument("--limit", type=int, default=0, help="Maximum rows (0 = all)")
    csv_parser.add_argument(
        "--preview", action="store_true",
        help="Print the selected queue without opening a browser",
    )
    csv_parser.add_argument(
        "--live", action="store_true",
        help="Actually submit; default is a non-submitting dry run",
    )
    csv_parser.add_argument(
        "--continue-on-failure", action="store_true",
        help="Continue after rows that need user intervention (default: stop)",
    )
    csv_parser.add_argument(
        "--hold-on-blocker", type=int, default=300, metavar="SECONDS",
        help="Keep the browser open after a live blocker (default: 300)",
    )

    # Workday login/session helpers
    workday_session_parser = subparsers.add_parser(
        "workday-session", help="Open a Workday login handoff session",
    )
    workday_session_parser.add_argument("url", help="Workday job or login URL")
    workday_session_parser.add_argument(
        "--browser", choices=("safari", "chromium"), default="",
        help="Safari for handoff or persistent Chromium for reusable automation state",
    )
    workday_session_parser.add_argument(
        "--hold", type=int, default=1800,
        help="Seconds to keep a Chromium login session open (default: 1800)",
    )

    workday_keychain_parser = subparsers.add_parser(
        "workday-keychain", help="Inspect or securely set a Workday Keychain credential",
    )
    workday_keychain_parser.add_argument("url", help="Workday URL used to identify the tenant")
    workday_keychain_parser.add_argument(
        "--action", choices=("status", "set"), default="status",
    )

    # stats
    subparsers.add_parser("stats", help="View application stats")

    # reset
    subparsers.add_parser("reset", help="Delete all jobs and start fresh")

    # rescore
    subparsers.add_parser("rescore", help="Re-score all unscored jobs")

    # server
    server_parser = subparsers.add_parser("server", help="Launch web dashboard")
    server_parser.add_argument("--port", type=int, default=8080, help="Port (default: 8080)")
    server_parser.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")

    args = parser.parse_args()

    if args.command == "stats":
        print_stats()
        return

    if args.command == "reset":
        cmd_reset()
        return

    profile = load_profile()

    if args.command == "discover":
        asyncio.run(cmd_discover(profile))
    elif args.command == "apply":
        dry_run = not args.live
        asyncio.run(cmd_apply(profile, dry_run=dry_run))
    elif args.command == "single":
        dry_run = not args.live
        if args.resume:
            resume = Path(args.resume).expanduser().resolve()
            if not resume.is_file():
                parser.error(f"Resume file not found: {resume}")
            profile["resume_path"] = str(resume)
        asyncio.run(cmd_single(
            profile, args.url, dry_run=dry_run,
            company=args.company, title=args.title,
        ))
    elif args.command == "apply-csv":
        if args.limit < 0:
            parser.error("--limit must be zero or greater")
        if args.hold_on_blocker < 0:
            parser.error("--hold-on-blocker must be zero or greater")
        asyncio.run(cmd_apply_csv(
            profile,
            args.csv_path,
            args.resume_dir,
            priorities=args.priorities,
            statuses=args.statuses,
            limit=args.limit,
            dry_run=not args.live,
            preview=args.preview,
            continue_on_failure=args.continue_on_failure,
            hold_on_blocker=args.hold_on_blocker,
        ))
    elif args.command == "workday-session":
        if args.hold < 0:
            parser.error("--hold must be zero or greater")
        preferred = profile.get("browser", {}).get("preferred_handoff_browser", "safari")
        asyncio.run(cmd_workday_session(profile, args.url, args.browser or preferred, args.hold))
    elif args.command == "workday-keychain":
        cmd_workday_keychain(profile, args.url, args.action)
    elif args.command == "rescore":
        asyncio.run(cmd_rescore(profile))
    elif args.command == "server":
        from dashboard.server import run_server
        try:
            from scheduler import setup_scheduler
            setup_scheduler()  # Configures jobs; actual start happens in FastAPI lifespan
        except Exception as e:
            print(f"  Scheduler setup warning: {e}")
        run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
