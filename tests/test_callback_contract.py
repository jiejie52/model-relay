from app.callback_contract import CALLBACK_CONTRACT_VERSION, callback_registration, derive_resume_query


def test_resume_query_mapping():
    assert derive_resume_query('fusion_corpus_ingest', {}, 'abc') == '/fusion resume abc'
    assert derive_resume_query('material_evidence_mapping', {}, 'abc') == '/fusion analyze'
    assert derive_resume_query('global_adjudication', {}, 'abc') == '/fusion analyze'
    assert derive_resume_query('scoped_decision', {'decision_text': 'C01=accept'}, 'abc') == '/fusion decide C01=accept'
    assert derive_resume_query('scoped_decision', {'decision_text': 'C01=accept'}, 'abc', continue_finalize=True) == '/fusion decide C01=accept\n/fusion finalize'
    assert derive_resume_query('synthesis_blueprint', {}, 'abc') == '/fusion finalize'
    assert derive_resume_query('quality_review', {}, 'abc') == '/fusion finalize'


def test_callback_registration_requires_exact_contract_and_owner():
    metadata = {
        'async_callback': {
            'contract_version': CALLBACK_CONTRACT_VERSION,
            'enabled': True,
            'dify_conversation_id': 'conv-1',
            'dify_user_id': 'user-1',
        }
    }
    row = callback_registration(
        metadata=metadata,
        stage='global_adjudication',
        payload={},
        job_id='job-1',
    )
    assert row['callback_status'] == 'waiting'
    assert row['callback_resume_query'] == '/fusion analyze'
    assert row['callback_conversation_id'] == 'conv-1'

    assert callback_registration(metadata={}, stage='global_adjudication', payload={}, job_id='job-1') == {}
    assert callback_registration(
        metadata={'async_callback': {'contract_version': CALLBACK_CONTRACT_VERSION, 'enabled': True}},
        stage='global_adjudication', payload={}, job_id='job-1'
    ) == {}
