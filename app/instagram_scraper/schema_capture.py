"""Browser-only diagnostic entry point: no persistence or pipeline imports."""
import time


def capture(profile, *, headless=False, partial_capture=False, post_observer=None, max_scrolls=None,
            collection=None, max_seconds=None):
    # sync_ins's DB adapters are lazy. Only pure response/browser helpers below
    # are used; neither normal main nor _scroll_profile_capture_and_upsert runs.
    from app.instagram_scraper import sync_ins as scraper
    if collection is not None and (profile!=collection.profile or partial_capture or post_observer is not None):
        raise ValueError('Isolated collection configuration required')
    if max_seconds is not None and (not isinstance(max_seconds,(int,float)) or not 0<max_seconds<=300):
        raise ValueError('Bounded capture duration required')
    if max_scrolls is not None and (type(max_scrolls) is not int or not 0<=max_scrolls<=(20 if collection is not None else 10)):
        raise ValueError('Bounded diagnostic scroll count required')
    partial=None
    if partial_capture:
        from app.instagram_scraper.partial_capture import PartialCapture
        partial=PartialCapture(profile)
    if post_observer is not None and partial is None:
        raise ValueError('Per-post observer requires isolated partial capture')
    health=scraper.new_capture_health()
    bodies=[];codes=set();started=time.monotonic()
    def result(reason=None, *, interrupted=False):
        failure=reason or scraper.capture_failure(health)
        health.update(status='failed' if failure else 'healthy',
                      reason=failure or 'timeline_verified')
        output=dict(status='interrupted' if interrupted else 'failed' if failure else 'completed',
                    mode='capture_only',database='not_used',database_writes=0,
                    gemini_http_requests=0,openai_generation_requests=0,
                    downstream='not_invoked',captured_posts=len(codes),scrape_health=health,
                    elapsed_seconds=round(max(0,time.monotonic()-started),3))
        if partial is not None:
            output['capture_outcome']=output['status']
            output['status']='partial_unverified'
            output['partial_capture']=partial.summary()
        return output
    account=profile.strip().lstrip('@')
    matches=[name for name in scraper.TARGET_URLS if name.casefold()==account.casefold()]
    if len(matches)!=1:return result('unknown_configured_profile')
    context=None;browser=None;page=None;listeners=[]
    reason=None;interrupted=False
    try:
        cookies=scraper.load_instagram_cookies_for_playwright()
        with scraper.sync_playwright() as runtime:
            try:
                # Ephemeral context: do not mutate the production persistent
                # Firefox profile, save screenshots, traces or storage state.
                browser=runtime.firefox.launch(headless=headless)
                context=browser.new_context(locale='en-US',viewport={'width':1280,'height':900})
                context.set_default_timeout(30_000)
                context.set_default_navigation_timeout(60_000)
                context.grant_permissions(['notifications'],origin=scraper.INSTAGRAM_ORIGIN)
                context.add_cookies(cookies)
                page=context.new_page()
                if collection is not None:
                    production_handler,_=scraper._make_graphql_posts_capture(
                        bodies,codes,health,diagnostic_payload=collection.observe)
                    def handler(response):
                        from app.instagram_scraper.request_profile_binding import classify
                        from app.instagram_scraper.query_transport import inspect_request
                        _,transport=inspect_request(response)
                        collection.request_diagnostics=dict(binding=classify(response,account),transport=transport)
                        try:production_handler(response)
                        finally:collection.request_diagnostics={}
                elif partial is None:
                    handler,_=scraper._make_graphql_posts_capture(bodies,codes,health)
                else:
                    def diagnostic_payload(payload,category):
                        partial.observe(payload,category)
                        if post_observer is not None:post_observer(payload,category)
                    production_handler,_=scraper._make_graphql_posts_capture(
                        bodies,codes,health,diagnostic_payload=diagnostic_payload)
                    def handler(response):
                        before_responses=health['graphql_responses'];before_errors=health['capture_errors']
                        try:
                            # Inspect requests only for the same Instagram GraphQL
                            # responses observed by production. Page URL/referrer
                            # and response author never establish target binding.
                            try:
                                parsed=scraper.urlparse(response.url)
                                graphql=(parsed.hostname in ('instagram.com','www.instagram.com')
                                         and 'graphql' in parsed.path.lower())
                            except Exception:graphql=False
                            if graphql:
                                with partial.request_context(response):production_handler(response)
                            else:production_handler(response)
                        finally:partial.response_finished(before_responses,before_errors,health)
                def request_failed(request):
                    try:
                        parsed=scraper.urlparse(request.url)
                        if parsed.hostname in ('instagram.com','www.instagram.com') and 'graphql' in parsed.path.lower():
                            scraper.capture_error(health,'request_failed')
                    except Exception:
                        scraper.capture_error(health,'response_processing_exception')
                listeners=[('response',handler),('requestfailed',request_failed)]
                for event,callback in listeners:page.on(event,callback)
                navigation=page.goto(f'{scraper.INSTAGRAM_ORIGIN}/{matches[0]}/',
                                     wait_until='domcontentloaded',timeout=60_000)
                page.wait_for_timeout(3000)
                reason=scraper.page_failure(page)
                if reason is None and (navigation is None or navigation.status>=400):
                    reason='navigation_blocked'
                if reason is None:
                    scraper._dismiss_instagram_notification_prompt_if_present(page)
                    page.wait_for_timeout(1500)
                    for _ in range(scraper.MAX_PROFILE_SCROLLS if max_scrolls is None else max_scrolls):
                        if max_seconds is not None and time.monotonic()-started>=max_seconds:
                            reason='capture_time_limit';break
                        if collection is not None and collection.boundary_reached:break
                        scraper._scroll_profile_down_once(page)
                        if collection is not None:
                            collection.counts['scrolls']+=1
                            collection.checkpoint('running','collecting_bounded_window')
                        reason=scraper.page_failure(page)
                        if reason:break
            finally:
                if page is not None:
                    for event,callback in listeners:
                        try:page.remove_listener(event,callback)
                        except Exception:scraper.capture_error(health,'browser_context_closed')
                for resource in (context,browser):
                    if resource is not None:
                        try:resource.close()
                        except Exception:scraper.capture_error(health,'browser_context_closed')
    except KeyboardInterrupt:
        reason='capture_interrupted';interrupted=True
    except (Exception,SystemExit):
        reason='capture_execution_failed'  # Never include exception messages.
    finally:
        bodies.clear()  # Captured response bodies never leave this stack or reach disk.
    return result(reason,interrupted=interrupted)
