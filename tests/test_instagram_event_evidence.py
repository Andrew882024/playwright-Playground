"""Offline evidence contract; no real OCR/AI or assertion of live NITRO mapping."""
import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace,ModuleType
import sys
import unittest
from unittest.mock import Mock,patch
from zoneinfo import ZoneInfo

from test_instagram_event_extraction import serial,ga,mt,row_six
from app.ai_analyze_service.event_evidence import EVIDENCE_INSTRUCTION,image_evidence_note
from app.instagram_event_master_projection import parent_marker


class EvidenceTests(unittest.TestCase):
    def event(self):
        result=row_six()['events'][0]
        result['field_sources']={'event_title':['first_image'],'start':['caption'],'location':['first_image','caption']}
        return result
    def test_complementary_first_image_caption_fields_do_not_require_both(self):
        # Synthetic already-merged model output, not a claim that local Python
        # reads image text. The real merge is requested of existing Gemini.
        for module in (ga,mt):
            event=module.ExtractedEventAI.model_validate(self.event())
            self.assertEqual(event.detection_status,'confirmed_event')
            self.assertEqual(event.event_title,'Coffee Chats')
            self.assertEqual(event.start.hour,10)
    def test_missing_fields_and_legacy_missing_evidence_are_potential(self):
        for field in ('location','start','field_sources'):
            value=self.event();value[field]=None if field!='field_sources' else {}
            self.assertEqual(ga.ExtractedEventAI.model_validate(value).detection_status,'potential_event')
        value=self.event();value['start']['minute']=None
        self.assertEqual(ga.ExtractedEventAI.model_validate(value).detection_status,'potential_event')
    def test_material_conflicts_are_distinct_from_missing_details(self):
        value=self.event();value.update(start=None,conflicting_fields=['start'])
        analysis=ga.PostAnalysisAI.model_validate(dict(description='',events=[value]))
        self.assertEqual(analysis.detection_status,'conflicting_information')
        self.assertEqual(analysis.events[0].detection_status,'conflicting_information')
        self.assertIsNone(analysis.events[0].start)
    def test_multiple_activities_remain_separate_and_zero_is_non_event(self):
        first=self.event();second=copy.deepcopy(first);second['event_title']='Second session'
        second['start']['hour']=17;second['end']['hour']=19
        for module in (ga,mt):
            analysis=module.PostAnalysisAI.model_validate(dict(description='',events=[first,second]))
            self.assertEqual(len(analysis.events),2);self.assertEqual(analysis.detection_status,'confirmed_event')
            self.assertEqual(module.PostAnalysisAI(events=[],description='').detection_status,'non_event')
    def test_evidence_metadata_does_not_change_database_projection(self):
        old=row_six();new=copy.deepcopy(old);new['events'][0].update(field_sources=self.event()['field_sources'])
        self.assertEqual(ga.ai_to_db_update_values(ga.PostAnalysisAI.model_validate(old),ZoneInfo('America/Los_Angeles')),
                         ga.ai_to_db_update_values(ga.PostAnalysisAI.model_validate(new),ZoneInfo('America/Los_Angeles')))
    def test_verified_first_reference_is_prioritized_before_image_cap(self):
        urls=[f'https://test.cdninstagram.com/{i}.jpg' for i in range(8)]
        post=dict(openable_media_urls={'largest':urls},first_image_url=urls[-1],image_order_verified=True)
        for module in (ga,mt):
            self.assertEqual(module._image_urls_from_post(post)[0],urls[-1])
            self.assertEqual(len(module._image_urls_from_post(post)),6)
            prompt=module._user_prompt(post,'reference',2,[urls[-1],urls[0]])
            self.assertIn('attachment 1 is the primary visual source',prompt)
    def test_legacy_order_and_failed_first_download_are_not_falsely_attested(self):
        post={'first_image_url':'https://test.cdninstagram.com/first.jpg','image_order_verified':True}
        self.assertIn('first image was not loaded',image_evidence_note(post,['https://test.cdninstagram.com/later.jpg']))
        self.assertIn('order is unverified',image_evidence_note({},['https://test.cdninstagram.com/later.jpg']))
    def test_caption_is_explicit_complement_not_replaced_by_thread_replies(self):
        row=SimpleNamespace(comments_json={'comments':[{'kind':'reply','text':'A reply'}]},
            additional_image_urls={},posted_unix_seconds=None,post_shortcode='synthetic',post_url='',
            post_title='',profile_username='synthetic',caption='Actual caption')
        post=serial.build_analyzer_post_dict(row)
        for module in (ga,mt):
            prompt=module._user_prompt(post,'ref',0,[])
            self.assertIn('Instagram caption (complementary evidence): Actual caption',prompt)
            self.assertIn('Do not require all three fields to appear in both sources',prompt)
            self.assertIn('Image-only/media completeness and source verification are separate',module._SYSTEM_INSTRUCTION)
            self.assertIn('do not claim first_image evidence',prompt.lower())
    def test_invalid_evidence_enums_are_rejected(self):
        from pydantic import ValidationError
        value=self.event();value['field_sources']['event_title']=['username_match']
        with self.assertRaises(ValidationError):ga.ExtractedEventAI.model_validate(value)
    def test_mock_existing_gemini_path_uses_same_request_and_model_contract(self):
        fixture=dict(description='',events=[self.event()])
        post={'shortcode':'synthetic','caption':'Date supplied in caption',
              'openable_media_urls':{'largest':['https://test.cdninstagram.com/source.jpg']},
              'first_image_url':'https://test.cdninstagram.com/source.jpg','image_order_verified':True}
        client=Mock()
        with patch.object(ga,'_fetch_image',return_value=(b'offline_image','image/png')), \
             patch.object(ga,'_generate_content_with_429_retry',return_value=SimpleNamespace(text=json.dumps(fixture))) as request:
            result,index=ga.analyze_post(client,['offline-model'],post,ZoneInfo('America/Los_Angeles'))
        self.assertEqual(result.detection_status,'confirmed_event');self.assertEqual(index,0)
        request.assert_called_once();client.models.generate_content.assert_not_called()


class NitroRegressionTests(unittest.TestCase):
    def test_nitro_mapping_has_dated_read_only_marker_evidence_not_only_user_claim(self):
        path=Path(__file__).parent/'fixtures/nitro_event_detection.json'
        fixture=json.loads(path.read_text())
        self.assertEqual(fixture['post_shortcode'],'DfQZTkcqcoR')
        self.assertEqual(fixture['user_reported_event_master_id'],16)
        self.assertEqual(fixture['marker_mapping_status'],'verified_read_only_snapshot')
        self.assertEqual(fixture['canonical_parent_marker'],parent_marker(1669))
        self.assertEqual(fixture['matched_event_ids'],[16])
        self.assertTrue(fixture['verified_at'])
        self.assertTrue(fixture['live_state_requires_reverification_before_future_execution'])
        for key in ('cover_regeneration_allowed','new_import_allowed','overwrite_allowed'):
            self.assertFalse(fixture[key])
    def test_actual_importer_dry_run_reuses_existing_marker_without_overwrite(self):
        # Review the real backend function without importing its configuration or
        # opening any engine. Rows and query results are entirely synthetic.
        path=Path(__file__).resolve().parents[3]/'cssa-activity-backend/instagram_parent_import.py'
        if not path.exists():self.skipTest('Adjacent backend not available')
        tree=ast.parse(path.read_text());node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='sync_record')
        from sqlalchemy.orm import DeclarativeBase,Mapped,mapped_column
        from sqlalchemy import Integer,Text
        class Base(DeclarativeBase):pass
        class Event(Base):
            __tablename__='offline_events'
            id:Mapped[int]=mapped_column(Integer,primary_key=True)
            event_description:Mapped[str]=mapped_column(Text)
        models=ModuleType('app.blueprint.dbBlueprint')
        for name in ('Events','Users','Roles','User_roles','Booking_ticket_action_info','Event_invitation_codes'):
            setattr(models,name,Event)
        namespace={'parent_marker':parent_marker,'ImportValidationError':ValueError}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
        # Parent 1669 is a SYNTHETIC mapping here, not a verified live NITRO row.
        marker=parent_marker(1669)
        event=SimpleNamespace(id=16,event_provider_id=1,event_title='NITRO',event_description=marker)
        db=Mock();db.scalars.side_effect=[Mock(all=Mock(return_value=[event])),Mock(all=Mock(return_value=[]))]
        before=vars(event).copy()
        with patch.dict(sys.modules,{'app.blueprint.dbBlueprint':models}):
            result=namespace['sync_record'](db,{'id':1669,'post_shortcode':'DfQZTkcqcoR'},[],{'event_title':'Different proposed title'},dry_run=True)
        self.assertEqual(result,dict(status='dry-run',action='update',event_id=16,post_id=1669))
        self.assertEqual(vars(event),before)
        db.add.assert_not_called();db.commit.assert_not_called();db.flush.assert_not_called()
        self.assertIn(marker,[value.replace('/_', '_') if isinstance(value,str) else value for value in db.scalars.call_args_list[0].args[0].compile().params.values()])

class SourceAvailabilityTests(unittest.TestCase):
    def test_model_cannot_claim_missing_first_image_or_missing_caption(self):
        from app.ai_analyze_service.event_evidence import constrain_sources
        value=EvidenceTests().event()
        original=ga.PostAnalysisAI(events=[ga.ExtractedEventAI.model_validate(value)],description='')
        result=constrain_sources(original,{},[],False)
        self.assertEqual(result.detection_status,'potential_event')
        self.assertTrue(all(not sources for sources in result.events[0].field_sources.values()))
        for field in ('event_title','start','location'):
            self.assertEqual(getattr(result.events[0],field),getattr(original.events[0],field))
        self.assertEqual(original.events[0].field_sources,value['field_sources'])
    def test_legacy_order_does_not_become_first_image_evidence(self):
        from app.ai_analyze_service.event_evidence import constrain_sources
        value=EvidenceTests().event()
        original=ga.PostAnalysisAI(events=[ga.ExtractedEventAI.model_validate(value)],description='')
        result=constrain_sources(original,{},['https://test.cdninstagram.com/legacy.jpg'],True)
        self.assertEqual(result.events[0].field_sources['event_title'],[])
        self.assertEqual(result.events[0].field_sources['location'],['caption'])
        self.assertEqual(result.detection_status,'potential_event')
