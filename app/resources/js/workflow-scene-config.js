(function () {
  'use strict';

  // Node IDs and edges come from the workflow snapshot. These profiles control presentation.
  window.workflowSceneConfig = {
    camera: { azimuth: -29, pitch: 42 },
    flowSpeed: 125,
    nodes: {
      __start__: { shape: 'gateway' },
      __end__: { shape: 'gateway' },
      'agentic:__start__': { shape: 'gateway' },
      'agentic:__end__': { shape: 'gateway' },
      node_search_embedding: { shape: 'stack' },
      node_search_embedding_hyde: { shape: 'stack' },
      node_web_search_mcp: { shape: 'gateway' },
      node_import_milvus: { shape: 'stack' },
      'agentic:bootstrap': { shape: 'stack' },
      publication: { shape: 'stack' },
      publish: { shape: 'stack' }
    },
    views: {
      import: {
        flat: [['__start__'], ['upload_file'], ['node_entry'], [null, null, 'node_pdf_to_md'],
          ['node_md_img'], ['node_document_split'], ['node_item_name_recognition'], ['node_bge_embedding'],
          ['node_import_milvus'], ['__end__']],
        space: [
          ['__start__', 'upload_file', 'node_entry'],
          [null, 'node_md_img', 'node_pdf_to_md'],
          ['node_bge_embedding', 'node_item_name_recognition', 'node_document_split'],
          ['node_import_milvus', '__end__']
        ]
      },
      query: {
        flat: [[null, '__start__'], [null, 'node_intent_route'],
          ['node_item_name_confirm', null, 'node_chitchat_answer'],
          ['node_agentic', 'node_search_embedding', 'node_search_embedding_hyde'],
          ['agentic:__start__', 'node_web_search_mcp'], ['agentic:bootstrap', 'node_rrf'],
          ['agentic:assess', 'node_rerank'], ['agentic:select', 'node_parent_expand'],
          ['agentic:execute', 'node_answer_output'], ['agentic:draft'], ['agentic:verify'],
          ['agentic:__end__'], [null, '__end__']],
        align: 1,
        space: [
          [null, null, '__start__'],
          [null, null, 'node_intent_route', 'node_chitchat_answer'],
          ['node_agentic', null, 'node_item_name_confirm'],
          ['agentic:__start__', null, 'node_search_embedding', 'node_search_embedding_hyde'],
          ['agentic:bootstrap', null, 'node_web_search_mcp', 'node_rrf'],
          ['agentic:assess', 'agentic:select', null, 'node_rerank'],
          ['agentic:draft', 'agentic:execute', null, 'node_parent_expand'],
          ['agentic:verify', null, null, 'node_answer_output'],
          ['agentic:__end__', null, '__end__']
        ],
        spacing: [194, 109]
      },
      'evolution-cycle': {
        space: [['__start__', 'publication', 'scan'], ['tuning', 'metrics', 'gaps'],
          ['backtest', '__end__']]
      },
      'evolution-candidate': {
        flat: [['feedback'], ['gap'], ['candidate', null, 'strategy'], ['supplement'],
          ['approval', null, 'reject'], ['publish'], ['withdraw'], ['__end__']],
        space: [
          [null, 'feedback'],
          [null, 'gap', 'strategy'],
          ['supplement', 'candidate'],
          [null, 'approval', 'reject'],
          [null, 'publish', 'withdraw'],
          [null, '__end__']
        ]
      }
    }
  };
})();
