"""Bounded read-only non-ARIA structural inspection; never membership evidence.

Explicit callers may use an already-open authenticated page on a future authorized
visit. Nothing here starts a browser, navigates, captures responses or writes.
No selectors, default div contract or production activation are inferred.
"""
from app.instagram_collaboration import account_name
from app.instagram_scraper.profile_grid_evidence import current_navigation

COUNTS = ('main', 'matching_headers', 'profile_navigation_links', 'selected_navigation_links',
          'visible_post_links', 'excluded_post_links', 'candidate_ancestors')
TAGS = ('div', 'section', 'article', 'other')
ATTRS = ('role', 'id', 'aria-labelledby', 'aria-controls', 'data-testid')
SELECTION = ('posts', 'reels', 'tagged', 'other', 'missing', 'ambiguous')
RELATIONS = ('adjacent_after', 'later_sibling', 'after_different_parent', 'before',
             'contains_navigation', 'inside_navigation', 'unavailable')
PATTERNS = ('plain_post', 'plain_reel', 'requested_profile_post', 'requested_profile_reel', 'other_profile_post')
FLAGS = ('contains_header', 'contains_excluded_region', 'contains_nonpost_links', 'branches_truncated')
NUMBERS = ('depth_from_main', 'post_links', 'other_profile_links', 'child_branches',
           'post_branches', 'minimum_links_per_post_branch', 'maximum_links_per_post_branch')


READ_STRUCTURE = r'''(profile) => {
  const visible = e => !!e && e.getClientRects().length > 0 &&
    getComputedStyle(e).display !== 'none' && getComputedStyle(e).visibility !== 'hidden';
  const one = a => a.length === 1 ? a[0] : null;
  const mains = Array.from(document.querySelectorAll('main,[role="main"]')).filter(visible);
  const main = one(mains);
  const counts = {main:mains.length,matching_headers:0,profile_navigation_links:0,
    selected_navigation_links:0,visible_post_links:0,excluded_post_links:0,candidate_ancestors:0};
  const result = {navigation_matches:location.protocol==='https:' &&
    ['instagram.com','www.instagram.com'].includes(location.hostname) && location.pathname===`/${profile}/`,
    counts,selection:'missing',navigation_kind:'missing',patterns:{},candidates:[],truncated:false};
  if(!main) return result;
  const headers=Array.from(main.querySelectorAll('header')).filter(h=>visible(h) &&
    Array.from(h.querySelectorAll('h1,h2,h3,[role="heading"]')).some(e=>
      visible(e) && e.textContent.trim().replace(/^@/,'').toLowerCase()===profile));
  counts.matching_headers=headers.length;
  const header=one(headers);
  const all=Array.from(main.querySelectorAll('a[href]'));
  result.truncated=all.length>500;
  const links=all.slice(0,500).filter(visible);
  const url = a => {
    try {const u=new URL(a.getAttribute('href'),location.origin);
      return u.protocol==='https:' && ['instagram.com','www.instagram.com'].includes(u.hostname) &&
        !u.username && !u.password && !u.search && !u.hash && (!u.port || u.port==='443') ? u:null;
    } catch(_){return null;}
  };
  const route = a => {
    const u=url(a);if(!u)return 'other';
    return u.pathname===`/${profile}/`?'posts':u.pathname===`/${profile}/reels/`?'reels':
      u.pathname===`/${profile}/tagged/`?'tagged':'other';
  };
  const navigationLinks=links.filter(a=>route(a)!=='other');
  const selected=navigationLinks.filter(a=>a.getAttribute('aria-selected')==='true');
  counts.profile_navigation_links=navigationLinks.length;
  counts.selected_navigation_links=selected.length;
  result.selection=selected.length===1?route(selected[0]):selected.length?'ambiguous':'missing';
  let navigation=null;
  if(selected.length===1) {
    navigation=selected[0].closest('[role="tablist"],nav');
    if(navigation && !main.contains(navigation))navigation=null;
    if(navigation)result.navigation_kind=navigation.matches('nav')?'nav':'tablist';
    else if(navigationLinks.length>=2) {
      let ancestor=selected[0].parentElement;
      for(let i=0;ancestor && ancestor!==main && i<8;i++,ancestor=ancestor.parentElement)
        if(navigationLinks.every(a=>ancestor.contains(a))){navigation=ancestor;break;}
      if(navigation)result.navigation_kind='common_ancestor';
    }
  }
  const pattern = a => {
    const u=url(a);if(!u)return null;
    if(/^\/p\/[A-Za-z0-9_-]{1,32}\/$/.test(u.pathname))return 'plain_post';
    if(/^\/reel\/[A-Za-z0-9_-]{1,32}\/$/.test(u.pathname))return 'plain_reel';
    const match=u.pathname.match(/^\/([A-Za-z0-9._]{1,30})\/(p|reel)\/[A-Za-z0-9_-]{1,32}\/$/);
    return match ? match[1].toLowerCase()===profile ?
      match[2]==='p'?'requested_profile_post':'requested_profile_reel':'other_profile_post':null;
  };
  const postLinks=links.filter(a=>pattern(a));
  counts.visible_post_links=postLinks.length;
  for(const a of postLinks){const kind=pattern(a);result.patterns[kind]=(result.patterns[kind]||0)+1;
    if(a.closest('aside,nav,[role="dialog"]'))counts.excluded_post_links++;}
  const ancestors=new Set();
  for(const a of postLinks.slice(0,128)) {
    let p=a.parentElement;
    for(let i=0;p&&p!==main&&i<12;i++,p=p.parentElement)
      if(['DIV','SECTION','ARTICLE'].includes(p.tagName)){
        if(ancestors.size<64)ancestors.add(p);else result.truncated=true;
      }
    if(p&&p!==main)result.truncated=true;
  }
  if(postLinks.length>128)result.truncated=true;
  counts.candidate_ancestors=ancestors.size;
  for(const element of ancestors){
    const contained=postLinks.filter(a=>element.contains(a));
    let relation='unavailable';
    if(navigation){
      if(element.contains(navigation))relation='contains_navigation';
      else if(navigation.contains(element))relation='inside_navigation';
      else if(navigation.nextElementSibling===element)relation='adjacent_after';
      else if(element.compareDocumentPosition(navigation)&Node.DOCUMENT_POSITION_FOLLOWING)relation='before';
      else relation=element.parentElement===navigation.parentElement?'later_sibling':'after_different_parent';
    }
    let depth=0;for(let p=element;p&&p!==main&&depth<32;p=p.parentElement)depth++;
    const allChildren=Array.from(element.children);
    const branches=allChildren.slice(0,100);
    const perBranch=branches.map(child=>contained.filter(a=>child===a||child.contains(a)).length).filter(n=>n>0);
    result.candidates.push({tag:element.tagName.toLowerCase(),relation,depth_from_main:depth,
      attributes:['role','id','aria-labelledby','aria-controls','data-testid'].filter(a=>element.hasAttribute(a)),
      contains_header:!!header&&(element.contains(header)||header.contains(element)),
      contains_excluded_region:!!element.closest('aside,nav,[role="dialog"]') ||
        !!element.querySelector('aside,nav,[role="dialog"]'),
      contains_nonpost_links:links.some(a=>element.contains(a)&&!pattern(a)),
      branches_truncated:allChildren.length>100,post_links:contained.length,
      other_profile_links:contained.filter(a=>pattern(a)==='other_profile_post').length,
      child_branches:allChildren.length,post_branches:perBranch.length,
      minimum_links_per_post_branch:perBranch.length?Math.min(...perBranch):0,
      maximum_links_per_post_branch:perBranch.length?Math.max(...perBranch):0});
  }
  return result;
}'''


def _count(value):
    return value if type(value) is int and 0 <= value <= 100000 else 0


def project_structure(raw):
    """Whitelist every output boundary. Anonymous topology is not a contract."""
    raw = raw if isinstance(raw, dict) else {}
    counts = raw.get('counts'); counts = counts if isinstance(counts, dict) else {}
    patterns = raw.get('patterns'); patterns = patterns if isinstance(patterns, dict) else {}
    result = dict(mode='structural_inspection_only', production_ingestion_enabled=False,
                  membership_evidence=False, selection=raw.get('selection') if raw.get('selection') in SELECTION else 'missing',
                  navigation_kind=raw.get('navigation_kind') if raw.get('navigation_kind') in
                    ('tablist','nav','common_ancestor') else 'missing',
                  counts={key:_count(counts.get(key)) for key in COUNTS},
                  patterns={key:_count(patterns.get(key)) for key in PATTERNS},
                  candidates=[],truncated=raw.get('truncated') is True)
    candidates = raw.get('candidates')
    if not isinstance(candidates, list):candidates=[]
    result['truncated'] |= len(candidates)>64
    for item in candidates[:64]:
        if not isinstance(item, dict):continue
        attrs = item.get('attributes'); attrs = attrs if isinstance(attrs, list) else []
        clean = dict(tag=item.get('tag') if item.get('tag') in TAGS else 'other',
                     relation=item.get('relation') if item.get('relation') in RELATIONS else 'unavailable',
                     attributes=[key for key in ATTRS if key in attrs],
                     fields_complete=(all(type(item.get(key)) is bool for key in FLAGS) and
                         all(type(item.get(key)) is int and 0 <= item[key] <= 100000 for key in NUMBERS)),
                     **{key:item.get(key) is True for key in FLAGS},
                     **{key:_count(item.get(key)) for key in NUMBERS})
        # Aggregate identical anonymous shapes; no node indices, classes, IDs or paths.
        old = next((p for p in result['candidates'] if all(p[key]==value for key,value in clean.items())), None)
        if old is None:result['candidates'].append(dict(clean,count=1))
        else:old['count']+=1
    plausible = [item for item in result['candidates'] if
        item['fields_complete'] and item['tag']!='other' and
        item['relation']=='adjacent_after' and item['post_branches']>=2 and item['post_links']>=2 and
        not any(item[key] for key in FLAGS) and item['other_profile_links']==0]
    if result['selection']!='posts':reason='posts_selection_unverified'
    elif result['counts']['main']!=1:reason='profile_main_unverified'
    elif result['counts']['matching_headers']!=1:reason='profile_header_unverified'
    elif result['counts']['selected_navigation_links']!=1 or result['navigation_kind']=='missing':reason='profile_navigation_unverified'
    elif result['truncated']:reason='structural_inspection_truncated'
    elif sum(item['count'] for item in plausible)>1:reason='ambiguous_container_candidates'
    elif plausible:reason='container_candidate_requires_review'
    else:reason='posts_boundary_unresolved'
    result['reason']=reason
    return result


def inspect_posts_structure(page, requested_profile):
    """Only reads an existing page; future live use requires separate authorization."""
    profile = account_name(requested_profile)
    if profile is None:return dict(project_structure({}), reason='invalid_requested_profile')
    try:
        before = current_navigation(page)
        if before is None or before.profile!=profile:
            return dict(project_structure({}), reason='navigation_context_unverified')
        raw = page.evaluate(READ_STRUCTURE, profile)
        after = current_navigation(page)
        if before!=after or not isinstance(raw,dict) or raw.get('navigation_matches') is not True:
            return dict(project_structure({}), reason='navigation_context_changed')
        return project_structure(raw)
    except Exception:
        return dict(project_structure({}), reason='structural_inspection_failed')
