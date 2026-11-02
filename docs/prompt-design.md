# Local analysis, prompt composition and style suggestions

Community v0.2 keeps article reading and image-prompt composition on the configured local Ollama model. The same `OllamaAdapter.prompts(slots, style, model)` path supplies a locally generated image, a cloud image API, or an approved local-agent image request. Image providers receive the frozen image prompt rather than the article. A failed local text request must stop approval; it does not authorize a cloud text model or a deterministic replacement prompt.

The public protocols in `backend/app/community_prompts.py` are original, editable source. They guide the model to describe a subject, known quantities and relationships, composition, lighting and material in a coherent scene. The model must preserve the user's director choices. Exact text, measurements, research findings and documentary claims cannot be invented to make an illustration look complete. Scientific and news imagery remains a generated illustration, including when it resembles a photograph.

The v0.1 `basic_cloud_prompts` serializer remains available to older integrations. It performs no model request and must not be called by the new approval path. Its output is not evidence of successful local-model composition.

## Categories and recommendations

The director offers fourteen choices. `inherit` follows the article's overall style. The remaining choices are editorial, photographic, watercolor, ink, flat, cinematic, three-dimensional illustration, diagram, scientific illustration, information design, architectural illustration, botanical illustration and paper cut. They are visual treatments, not separate claims about model accuracy or supported scientific tasks.

Fresh local analysis asks for a `style_recommendation` alongside each candidate:

```json
{
  "style_id": "botanical",
  "reason": "以清晰轮廓突出植物形态，保留原文已知特征。",
  "source": "local_model"
}
```

The model chooses a supported category using the corresponding paragraph and planned scene. The backend publishes an original short explanation for that category instead of echoing the model's free-form reason. The output schema constrains categories; the runtime separately validates them. Missing or malformed recommendations use bounded semantic cues from the same paragraph and scene, with genre as a last fallback. These are labelled `source: "semantic_rules"` and their reason starts with `规则推荐：`. Such a fallback is a style suggestion only. It does not stand in for successful article analysis or prompt composition.

The frontend marks the suggested option `（推荐）`. Fresh slots can start with that category as their director style. The user can still choose any supported style, including `inherit`. Existing directors are not overwritten. Reading an old task does not add recommendation metadata, and already approved snapshots are not recomposed.

## Objects, backgrounds and approval

The local model uses a bounded JSON schema for every scene. Director choices add provider-neutral guidance to its input. A slot with objects also gets separate local translations for each approved object's own features; an explicit background gets its own translation. Shared scene drafts do not substitute for failed object or background requests. Positive scene descriptions and exclusion terms remain separate.

Fixed style, lighting and placement controls are compiled before approval. This deterministic compilation makes approved controls explicit; it does not replace the local model's scene composition. The approved snapshot binds the exact resulting prompt. The later image provider or agent may not rewrite it.

When a generated figure is inserted into an article, its numbered caption includes the parenthetical AI-generated label in the insertion preview and each export format. No additional duplicate disclosure paragraph is added. Original source media and zero-image plans receive no added AI label.

Request evidence distinguishes a started request, a completed response, and a validated result. Invalid JSON, missing scene IDs, unusable English or failed local calls stop the operation. Mock tests validate these boundaries; they are not evidence of real image quality. A real local-model sample and an image displayed in the application are separate acceptance steps.

See [design-method provenance](prompt-method-provenance.md) for the limited local reference review and licensing boundaries.
