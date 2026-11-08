"use strict";
// Stream text is a candidate only. Publication and citation links come from task results.
globalThis.RepoStream = {
  create({token, isRunning, onDraft}) {
    const drafts = new Map(), subscriptions = new Map();
    function apply(identifier, event) {
      const prior = drafts.get(identifier) || {revision: 0, text: ''};
      const revision = event.candidate_revision ?? prior.revision;
      if (revision < prior.revision) return;
      const current = revision > prior.revision ? {revision, text: ''} : {...prior};
      if (event.event === 'snapshot') current.text = (event.text || '').slice(0, 100000);
      else if (event.event === 'draft_delta') current.text = (current.text + (event.text || '')).slice(0, 100000);
      else if (event.event === 'draft_start') current.text = '';
      drafts.set(identifier, current);
      if (['draft_start', 'draft_delta', 'snapshot'].includes(event.event)) onDraft(identifier, current);
    }
    function follow(identifier) {
      if (subscriptions.has(identifier) || typeof TextDecoder === 'undefined') return;
      const controller = new AbortController();
      subscriptions.set(identifier, controller);
      (async () => {
        let after = 0;
        try {
          while (isRunning(identifier)) {
            const response = await fetch(`/api/tasks/${identifier}/events?after=${after}`, {
              headers: {'X-RepoAgent-Token': token}, signal: controller.signal
            });
            if (!response.ok || !response.body?.getReader) return;
            const reader = response.body.getReader(), decoder = new TextDecoder();
            let buffer = '', terminal = false;
            while (true) {
              const chunk = await reader.read();
              if (chunk.done) break;
              buffer += decoder.decode(chunk.value, {stream: true});
              if (buffer.length > 500000) throw new Error('stream capacity');
              let end;
              while ((end = buffer.indexOf('\n')) >= 0) {
                const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
                if (!line) continue;
                const event = JSON.parse(line);
                // A reconnect may replay an event; appending the same delta twice is invalid.
                if (event.sequence && event.sequence <= after) continue;
                after = Math.max(after, event.sequence || 0);
                apply(identifier, event);
                terminal ||= event.terminal || ['published', 'failed'].includes(event.event);
              }
            }
            if (terminal) return;
            await new Promise(resolve => setTimeout(resolve, 500));
          }
        } catch { /* The task poll remains the source of final publication status. */ }
        finally { subscriptions.delete(identifier); }
      })();
    }
    function finish(identifier) {
      subscriptions.get(identifier)?.abort();
      drafts.delete(identifier);
    }
    return {drafts, follow, apply, finish};
  }
};
