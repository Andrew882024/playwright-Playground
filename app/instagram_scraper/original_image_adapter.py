"""Offline original-image reference recovery; never a production trust adapter.

Supports the observed XDTMediaDict nullable layout without modifying _kind,
_normalize, raw media, source/identity checks, or ingestion eligibility. Outputs
are private references, not evidence of authenticated source or byte availability.
"""
from dataclasses import dataclass,field
from urllib.parse import urlsplit
from app.instagram_scraper.post_ingestion_policy import _usable_image


class ImageRecoveryError(ValueError):
    """Fixed authored reason codes only."""


@dataclass(frozen=True)
class OriginalImages:
    image_urls:tuple[str,...]=field(repr=False)
    layout:str
    variant_count:int
    # This describes ordering within the supplied media only, NOT source proof.
    original_order_basis:str
    production_eligible:bool=False

    @property
    def first_image_url(self):return self.image_urls[0]

    def inspection(self):
        return dict(status='references_recovered',image_count=len(self.image_urls),
            variant_count=self.variant_count,layout=self.layout,
            original_order_basis=self.original_order_basis,
            source_membership_verified=False,media_identity_verified=False,
            graphql_integrity_verified=False,production_eligible=False,
            reference_bytes_fetched=False)


def _type(media):
    if not isinstance(media,dict):raise ImageRecoveryError('malformed_media')
    number=media.get('media_type');name=media.get('__typename')
    if 'media_type' in media and (type(number) is not int or number not in (1,2,8)):
        raise ImageRecoveryError('unknown_media_type')
    if name not in (None,'GraphImage','GraphVideo','GraphSidecar','XDTMediaDict'):
        raise ImageRecoveryError('unsupported_media_layout')
    named={'GraphImage':1,'GraphVideo':2,'GraphSidecar':8}.get(name)
    if named is not None and number is not None and number!=named:
        raise ImageRecoveryError('conflicting_media_type')
    kind=number if number is not None else named
    if kind==2:raise ImageRecoveryError('video_excluded')
    if kind not in (1,8):raise ImageRecoveryError('unknown_media_type')
    if 'is_video' in media and media['is_video'] is not False:
        raise ImageRecoveryError('video_or_unknown_media')
    for key in ('video_url','video_versions','video_dash_manifest','clips_metadata'):
        # Only explicitly observed XDT null placeholders are supported. Empty
        # lists/objects or non-null video fields are not assumed harmless.
        if key in media and not (name=='XDTMediaDict' and key in ('video_versions','video_dash_manifest','clips_metadata') and media[key] is None):
            raise ImageRecoveryError('video_or_unknown_media')
    if media.get('product_type') not in (None,'feed','carousel_container'):
        raise ImageRecoveryError('unsupported_product_type')
    return kind


def _one_image(media):
    # Same post-attached positions used by the historical scraper, not avatars,
    # comments, generated covers, or recursive arbitrary URL discovery.
    from app.instagram_scraper.sync_ins import _instagram_url_asset_key, _post_media_cdn_urls_from_timeline_node
    variants=[]
    def add(url,width=None,height=None):
        if not _usable_image(url):raise ImageRecoveryError('invalid_image_reference')
        if not urlsplit(url).path.lower().endswith(('.jpg','.jpeg','.png','.webp')):
            raise ImageRecoveryError('unsupported_image_reference')
        if (width is None)!=(height is None):raise ImageRecoveryError('invalid_image_dimensions')
        if width is not None and (type(width) is not int or type(height) is not int or
                                  not 0<width<=100000 or not 0<height<=100000):
            raise ImageRecoveryError('invalid_image_dimensions')
        variants.append((url,(width or 0)*(height or 0)))
        if len(variants)>256:raise ImageRecoveryError('image_reference_limit')
    for key in ('display_url','thumbnail_src'):
        if key in media:add(media[key])
    for key in ('image_versions2','image_versions'):
        if key not in media:continue
        node=media[key];items=node.get('candidates') if isinstance(node,dict) else None
        if not isinstance(items,list) or not 1<=len(items)<=128:raise ImageRecoveryError('incomplete_image_references')
        for item in items:
            if not isinstance(item,dict):raise ImageRecoveryError('incomplete_image_references')
            add(item.get('url'),item.get('width'),item.get('height'))
    if 'display_resources' in media:
        items=media['display_resources']
        if not isinstance(items,list) or not 1<=len(items)<=128:raise ImageRecoveryError('incomplete_image_references')
        for item in items:
            if not isinstance(item,dict):raise ImageRecoveryError('incomplete_image_references')
            add(item.get('src'),item.get('config_width'),item.get('config_height'))
    if not variants:raise ImageRecoveryError('missing_original_image_reference')
    # Reuse the successful scraper's post-only extraction, restricted to the
    # reviewed positions above; never its broad fallback or sorted post bundle.
    projection={key:media[key] for key in ('display_url','thumbnail_src') if key in media}
    for key in ('image_versions2','image_versions'):
        if key in media:
            projection[key]={'candidates':[{'url':item['url']} for item in media[key]['candidates']]}
    if 'display_resources' in media:
        projection['display_resources']=[{'src':item['src']} for item in media['display_resources']]
    legacy_urls=_post_media_cdn_urls_from_timeline_node(projection)
    if {url for url,_ in variants}!=legacy_urls:
        raise ImageRecoveryError('legacy_image_reference_mismatch')
    # Reuse the legacy asset key, but never sort assets across carousel slides.
    # Multiple asset groups in a declared single-image node are ambiguous.
    if len({_instagram_url_asset_key(url) for url,_ in variants})!=1:
        raise ImageRecoveryError('conflicting_image_assets')
    selected=max(variants,key=lambda item:item[1])[0]  # stable tie: source candidate order
    return selected,len(variants)


def recover_original_images(media):
    kind=_type(media)
    if kind==1:
        if 'edge_sidecar_to_children' in media:raise ImageRecoveryError('conflicting_carousel_layout')
        if 'carousel_media' in media and not (media.get('__typename')=='XDTMediaDict' and media['carousel_media'] is None):
            raise ImageRecoveryError('conflicting_carousel_layout')
        count=media.get('carousel_media_count')
        if count is not None and (type(count) is not int or count!=0):raise ImageRecoveryError('conflicting_carousel_count')
        url,variants=_one_image(media)
        return OriginalImages((url,),'single_image',variants,'explicit_single_image_one_asset')
    if 'carousel_media' in media and 'edge_sidecar_to_children' in media:
        raise ImageRecoveryError('conflicting_carousel_layout')
    if 'carousel_media' in media:
        children=media['carousel_media'];count=media.get('carousel_media_count')
    else:
        connection=media.get('edge_sidecar_to_children')
        if not isinstance(connection,dict):raise ImageRecoveryError('incomplete_carousel')
        edges=connection.get('edges');page=connection.get('page_info')
        if (not isinstance(edges,list) or not isinstance(page,dict) or page.get('has_next_page') is not False):
            raise ImageRecoveryError('incomplete_carousel')
        children=[edge.get('node') if isinstance(edge,dict) else None for edge in edges]
        count=connection.get('count')
    if not isinstance(children,list) or not 1<=len(children)<=20 or type(count) is not int or count!=len(children):
        raise ImageRecoveryError('incomplete_carousel')
    urls=[];variant_count=0;seen=set()
    from app.instagram_scraper.sync_ins import _instagram_url_asset_key
    for child in children:
        if _type(child)!=1:raise ImageRecoveryError('non_image_carousel_child')
        # Reuse single-image checks; preserve the original child ARRAY order.
        recovered=recover_original_images(child)
        key=_instagram_url_asset_key(recovered.first_image_url)
        if key in seen:raise ImageRecoveryError('duplicate_carousel_asset')
        seen.add(key);urls.append(recovered.first_image_url);variant_count+=recovered.variant_count
    return OriginalImages(tuple(urls),'image_only_carousel',variant_count,'explicit_carousel_child_array')
