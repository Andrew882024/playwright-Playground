"""One existing Instagram parent. Default preflight; --run authorizes bounded writes/API calls."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-id',type=int,required=True)
    from app.ai_analyze_service.model_selection import validate_model
    def model_arg(value):
        try:return validate_model(value)
        except ValueError:raise argparse.ArgumentTypeError('Expected a Gemini model ID.') from None
    parser.add_argument('--gemini-model',type=model_arg,
                        help='Override this selected parent only; disables model fallback')
    parser.add_argument('--gemini-max-http-requests',type=int,
                        help='Optional strict generation-request budget; failed/uncertain requests are never retried')
    parser.add_argument('--authorize-gemini-retry',action='store_true',
                        help='Explicitly authorize one new request after reviewing earlier attempts')
    parser.add_argument('--confirm-gemini-retry',action='store_true',
                        help='Separate confirmation that a new Gemini request may incur another charge')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--preflight',action='store_true')
    mode.add_argument('--run',action='store_true')
    args=parser.parse_args(argv)
    if args.parent_id<=0:parser.error('parent-id must be positive')
    if args.gemini_max_http_requests is not None and args.gemini_max_http_requests<0:
        parser.error('gemini-max-http-requests must be nonnegative')
    if args.authorize_gemini_retry!=args.confirm_gemini_retry:
        parser.error('Gemini retry requires both --authorize-gemini-retry and --confirm-gemini-retry')
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent/'.env')
    from app.parent_pipeline.service import preflight,run
    try:
        kwargs={} if args.gemini_max_http_requests is None else dict(gemini_max_http_requests=args.gemini_max_http_requests)
        if args.gemini_model is not None:
            kwargs['gemini_model']=args.gemini_model
        if args.authorize_gemini_retry:
            kwargs.update(authorize_gemini_retry=True,confirm_gemini_retry=True)
        result=run(args.parent_id,**kwargs) if args.run else preflight(args.parent_id)
        if not args.run:
            result.update(gemini_max_http_requests=args.gemini_max_http_requests,gemini_http_requests=0)
            if args.gemini_model is not None:
                result.update(gemini_model_override=args.gemini_model,gemini_automatic_model_fallback=False)
    except (Exception,SystemExit):result=dict(status='failed',reason='parent_pipeline_configuration_or_database_unavailable')
    print(json.dumps(result,sort_keys=True))
    return 0 if result['status'] in ('preflight','completed','no-op') else 1

if __name__=='__main__':raise SystemExit(main())
