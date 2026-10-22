import httpx
import os
import base64
import json
from typing import Dict, Any, Optional
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ai_config import ai_config, AIAnalysisMode

from artrack.config import settings

INTERNAL_API_KEY = settings.AI_INTERNAL_API_KEY
API_BASE_URL = "http://127.0.0.1:8001"  # Internal communication

async def analyze_content_with_chatgpt(
    data: bytes,
    mime_type: str,
    context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Analyzes content (text or image) using the internal Gemini API.

    Supports two modes based on AI_ANALYSIS_MODE environment variable:
    - UNIFIED (default): Single AI request for all analysis (faster, cheaper)
    - SPLIT: Separate requests for safety check and embedding generation (flexible model selection)

    Args:
        data: File content as bytes
        mime_type: MIME type of the file
        context: Optional contextual metadata to improve AI analysis

    Returns:
        Comprehensive dict with safety_info, category, tags, collections, and extracted_tags
    """
    # Route to appropriate analysis mode
    if ai_config.is_split():
        return await _analyze_split_mode(data, mime_type, context)
    else:
        return await _analyze_unified_mode(data, mime_type, context)


async def _analyze_unified_mode(
    data: bytes,
    mime_type: str,
    context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    UNIFIED MODE: Single AI request for comprehensive analysis.
    Fast and cost-effective - one API call returns everything.
    """
    # Build context-aware prompt
    context_info = ""
    if context:
        context_parts = []

        # File path context (folder structure reveals semantic organization)
        if "file_path" in context and context["file_path"]:
            context_parts.append(f"File path: {context['file_path']}")

        # Collection context
        if "collection_id" in context and context["collection_id"]:
            context_parts.append(f"Collection: {context['collection_id']}")

        # Semantic metadata
        if "metadata" in context and context["metadata"]:
            meta = context["metadata"]
            if isinstance(meta, dict):
                meta_str = ", ".join([f"{k}: {v}" for k, v in meta.items()])
                context_parts.append(f"Metadata: {meta_str}")

        # Media role
        if "role" in context and context["role"]:
            context_parts.append(f"Role: {context['role']}")

        # Unstructured context text (for AI to parse and extract structured data)
        if "context_text" in context and context["context_text"]:
            context_parts.append(f"\nFree-form description:\n{context['context_text']}")

        if context_parts:
            context_info = "\n\nContextual Information:\n" + "\n".join(context_parts) + "\n"

    # Unified comprehensive prompt that returns ALL analysis data in one request
    prompt_text = f"""
Analysiere diesen Inhalt und gib eine umfassende strukturierte Antwort zurück.
{context_info}

DEINE AUFGABEN:

1. SAFETY CHECK (Sicherheitsbewertung)
   - Überprüfe auf NSFW, Gewalt, inappropriate Inhalte
   - Bewerte die Sicherheit (true/false)
   - Gib einen Konfidenz-Wert (0.0-1.0)
   - Erkläre deine Einschätzung kurz
   - Liste eventuelle Probleme in 'flags' auf

2. CLASSIFICATION (Kategorisierung)
   - Klassifiziere in eine Kategorie: 'product', 'person', 'event', 'landscape', 'art', 'document', 'video', 'text', 'other'
   - Bewerte das Gefährdungspotenzial für Kinder (1-10 Skala: 1=völlig sicher, 10=sehr gefährlich)

3. CONTENT ANALYSIS (Inhaltsanalyse)
   - Generiere einen prägnanten Titel (max 50 Zeichen)
   - Erstelle einen emotionalen Untertitel mit Emoji (Instagram-Style)
   - Erkenne 3-5 relevante Tags (OHNE #-Symbol, auf deutsch)
   - Schlage 1-2 einfache Collection-Namen vor (z.B. "Arbeit", "Freizeit", "Familie", "Reisen")

4. KNOWLEDGE GRAPH EXTRACTION (Strukturierte Semantic-Daten)
   - Extrahiere ALLE relevanten strukturierten Daten basierend auf Content-Type
   - Wähle die Felder intelligent basierend auf dem Inhaltstyp:
     * Products: brand, product, year, colors, sizes, materials, certifications, price_range
     * Events: event_name, location, date, year, participants, event_type
     * People: names, age_range, gender, occupation, relationships
     * Landscapes: location, country, region, time_of_day, weather, season
     * Videos/Media: title, creator, date, location, duration_description, subjects
     * Documents: doc_type, author, date, subject, language
   - Füge IMMER ein 'keywords' Array mit semantischen Keywords hinzu

WICHTIG: Füge NUR Felder hinzu, die wirklich relevant für diesen spezifischen Inhalt sind!

ANTWORT FORMAT (EXAKT dieses JSON Format zurückgeben):
{{
  "safetyCheck": {{
    "isSafe": boolean,
    "confidence": number (0.0-1.0),
    "reasoning": "string",
    "flags": []
  }},
  "classification": {{
    "category": "string",
    "dangerPotential": integer (1-10)
  }},
  "mediaAnalysis": {{
    "suggestedTitle": "string (max 50 chars)",
    "suggestedSubtitle": "string with emoji",
    "tags": ["tag1", "tag2", "tag3"],
    "collectionSuggestions": ["collection1", "collection2"]
  }},
  "extractedTags": {{
    // Dynamic structured fields based on content type
    "keywords": ["keyword1", "keyword2"]  // ALWAYS include
  }},
  "embeddingInfo": {{
    "embeddingText": "string - Rich searchable text combining all key info for semantic search",
    "searchableFields": ["field1", "field2"],  // Which fields should be searchable
    "metadata": {{}}  // Structured metadata for filtering
  }}
}}

BEISPIEL für Produkt:
{{
  "safetyCheck": {{"isSafe": true, "confidence": 1.0, "reasoning": "Produktbeschreibung ohne problematische Inhalte", "flags": []}},
  "classification": {{"category": "product", "dangerPotential": 1}},
  "mediaAnalysis": {{
    "suggestedTitle": "O'Neal Airframe MX Helm",
    "suggestedSubtitle": "Maximaler Schutz für deine Abenteuer 🏍️✨",
    "tags": ["helm", "motocross", "sicherheit", "ausrüstung"],
    "collectionSuggestions": ["Motorsport", "Ausrüstung"]
  }},
  "extractedTags": {{
    "brand": "O'Neal",
    "product": "Airframe MX Helm",
    "year": 2026,
    "colors": ["black", "red"],
    "materials": ["fiberglass"],
    "certifications": ["ECE 22.05"],
    "keywords": ["helmet", "motocross", "safety", "mx", "protection"]
  }},
  "embeddingInfo": {{
    "embeddingText": "O'Neal Airframe MX Helm 2026 - Premium Motocross Helmet. Leichte Fiberglas-Schale mit optimaler Belüftung und ECE 22.05 Zertifizierung. Erhältlich in schwarz und rot. Ideal für professionelle Motocross-Fahrer die maximalen Schutz und Komfort suchen.",
    "searchableFields": ["brand", "product", "year", "materials", "certifications", "keywords"],
    "metadata": {{
      "brand": "O'Neal",
      "category": "helmet",
      "sport": "motocross",
      "year": 2026,
      "certified": true
    }}
  }}
}}
"""

    # Prepare content for analysis
    images_list = []

    if mime_type.startswith("image/"):
        encoded_image = base64.b64encode(data).decode("utf-8")
        images_list.append(encoded_image)
    elif mime_type.startswith("text/"):
        try:
            content_as_text = data.decode("utf-8")
            separator = "\n\n--- CONTENT ---\n"
            prompt_text += separator + content_as_text[:8000]
        except UnicodeDecodeError:
            return {
                "category": "binary",
                "danger_potential": 1,
                "safety_info": {"isSafe": True, "confidence": 1.0, "reasoning": "Binary file", "flags": []}
            }
    else:
        return {
            "category": "other",
            "danger_potential": 1,
            "safety_info": {"isSafe": True, "confidence": 1.0, "reasoning": "Unsupported type", "flags": []}
        }

    # Use Gemini API for comprehensive analysis
    payload = {
        "prompt": {
            "text": prompt_text,
            "images": images_list
        }
    }

    headers = {
        "X-API-KEY": INTERNAL_API_KEY,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{API_BASE_URL}/ai/gemini",
                json=payload,
                headers=headers,
                timeout=60.0
            )
            response.raise_for_status()

            ai_response_str = response.json().get("message", "{}")

            # Clean JSON markers
            ai_response_str = ai_response_str.strip()
            if ai_response_str.startswith("```json"):
                ai_response_str = ai_response_str[7:].strip()
            if ai_response_str.startswith("```"):
                ai_response_str = ai_response_str[3:].strip()
            if ai_response_str.endswith("```"):
                ai_response_str = ai_response_str[:-3].strip()

            analysis_result = json.loads(ai_response_str)

            # Extract all components from comprehensive response
            safety_check = analysis_result.get("safetyCheck", {})
            classification = analysis_result.get("classification", {})
            media_analysis = analysis_result.get("mediaAnalysis", {})
            extracted_tags = analysis_result.get("extractedTags", {})
            embedding_info = analysis_result.get("embeddingInfo", {})

            # Return unified comprehensive response
            return {
                # Classification data
                "category": classification.get("category", "unknown"),
                "danger_potential": classification.get("dangerPotential", 1),

                # Safety information (detailed)
                "safety_info": {
                    "isSafe": safety_check.get("isSafe", True),
                    "confidence": safety_check.get("confidence", 1.0),
                    "reasoning": safety_check.get("reasoning", ""),
                    "flags": safety_check.get("flags", [])
                },

                # Media analysis data
                "ai_title": media_analysis.get("suggestedTitle"),
                "ai_subtitle": media_analysis.get("suggestedSubtitle"),
                "ai_tags": media_analysis.get("tags", []),
                "ai_collections": media_analysis.get("collectionSuggestions", []),

                # Knowledge Graph extraction (structured semantic data)
                "extracted_tags": extracted_tags,

                # Embedding generation instructions for KG pipeline
                "embedding_info": embedding_info,

                # Debug information
                "prompt": prompt_text,
                "ai_response": ai_response_str
            }

        except (httpx.RequestError, json.JSONDecodeError, TypeError, ValueError) as e:
            print(f"AI analysis failed: {e}")
            import traceback
            traceback.print_exc()
            return {
                "category": "error",
                "danger_potential": 1,
                "safety_info": {"isSafe": True, "confidence": 0.5, "reasoning": f"Analysis failed: {e}", "flags": []}
            }


async def _analyze_split_mode(
    data: bytes,
    mime_type: str,
    context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    SPLIT MODE: Separate AI requests for safety check and embedding generation.
    Allows using different models for each task (e.g., fast model for safety, powerful model for embeddings).

    Uses environment variables:
    - SAFETY_MODEL: Model for safety checks
    - EMBEDDING_MODEL: Model for embedding generation
    """
    print(f"🔄 SPLIT MODE: Running safety check with {ai_config.safety_model} and embedding generation with {ai_config.embedding_model}")

    # Prepare content for both requests
    images_list = []
    text_content = None

    if mime_type.startswith("image/"):
        encoded_image = base64.b64encode(data).decode("utf-8")
        images_list.append(encoded_image)
    elif mime_type.startswith("text/"):
        try:
            text_content = data.decode("utf-8")[:8000]
        except UnicodeDecodeError:
            return {
                "category": "binary",
                "danger_potential": 1,
                "safety_info": {"isSafe": True, "confidence": 1.0, "reasoning": "Binary file", "flags": []}
            }
    else:
        return {
            "category": "other",
            "danger_potential": 1,
            "safety_info": {"isSafe": True, "confidence": 1.0, "reasoning": "Unsupported type", "flags": []}
        }

    # Build context info (shared between both requests)
    context_info = _build_context_info(context)

    # STEP 1: Safety Check (fast, basic classification)
    safety_result = await _run_safety_check(images_list, text_content, context_info)

    # STEP 2: Embedding Generation (comprehensive semantic analysis)
    embedding_result = await _run_embedding_generation(images_list, text_content, context_info)

    # Merge results from both requests
    return {
        # From safety check
        "category": safety_result.get("category", "unknown"),
        "danger_potential": safety_result.get("danger_potential", 1),
        "safety_info": safety_result.get("safety_info", {}),

        # From embedding generation
        "ai_title": embedding_result.get("ai_title"),
        "ai_subtitle": embedding_result.get("ai_subtitle"),
        "ai_tags": embedding_result.get("ai_tags", []),
        "ai_collections": embedding_result.get("ai_collections", []),
        "extracted_tags": embedding_result.get("extracted_tags", {}),
        "embedding_info": embedding_result.get("embedding_info", {}),

        # Debug info
        "prompt": f"SPLIT MODE: Safety={ai_config.safety_model}, Embedding={ai_config.embedding_model}",
        "ai_response": "Split mode: combined results"
    }


def _build_context_info(context: Optional[Dict[str, Any]]) -> str:
    """Build context information string from context dict"""
    context_info = ""
    if context:
        context_parts = []

        if "file_path" in context and context["file_path"]:
            context_parts.append(f"File path: {context['file_path']}")

        if "collection_id" in context and context["collection_id"]:
            context_parts.append(f"Collection: {context['collection_id']}")

        if "metadata" in context and context["metadata"]:
            meta = context["metadata"]
            if isinstance(meta, dict):
                meta_str = ", ".join([f"{k}: {v}" for k, v in meta.items()])
                context_parts.append(f"Metadata: {meta_str}")

        if "role" in context and context["role"]:
            context_parts.append(f"Role: {context['role']}")

        if "context_text" in context and context["context_text"]:
            context_parts.append(f"\nFree-form description:\n{context['context_text']}")

        if context_parts:
            context_info = "\n\nContextual Information:\n" + "\n".join(context_parts) + "\n"

    return context_info


async def _run_safety_check(
    images_list: list,
    text_content: Optional[str],
    context_info: str
) -> Dict[str, Any]:
    """
    Run safety check only (fast, focused on content moderation)
    Model: SAFETY_MODEL (default: gemini-flash)
    """
    prompt = f"""
Analysiere diesen Inhalt hinsichtlich Sicherheit und grundlegender Kategorisierung.
{context_info}

AUFGABEN:
1. SAFETY CHECK - Überprüfe auf NSFW, Gewalt, inappropriate Inhalte
2. CLASSIFICATION - Kategorisiere in: 'product', 'person', 'event', 'landscape', 'art', 'document', 'video', 'text', 'other'
3. DANGER POTENTIAL - Gefährdungspotenzial für Kinder (1-10)

ANTWORT FORMAT (JSON):
{{
  "safetyCheck": {{
    "isSafe": boolean,
    "confidence": number (0.0-1.0),
    "reasoning": "string",
    "flags": []
  }},
  "classification": {{
    "category": "string",
    "dangerPotential": integer (1-10)
  }}
}}
"""

    if text_content:
        prompt += f"\n\n--- CONTENT ---\n{text_content}"

    payload = {
        "prompt": {"text": prompt, "images": images_list}
    }

    headers = {
        "X-API-KEY": INTERNAL_API_KEY,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{API_BASE_URL}/ai/gemini",
                json=payload,
                headers=headers,
                timeout=30.0
            )
            response.raise_for_status()

            ai_response_str = response.json().get("message", "{}")
            ai_response_str = ai_response_str.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(ai_response_str)

            safety_check = result.get("safetyCheck", {})
            classification = result.get("classification", {})

            return {
                "category": classification.get("category", "unknown"),
                "danger_potential": classification.get("dangerPotential", 1),
                "safety_info": {
                    "isSafe": safety_check.get("isSafe", True),
                    "confidence": safety_check.get("confidence", 1.0),
                    "reasoning": safety_check.get("reasoning", ""),
                    "flags": safety_check.get("flags", [])
                }
            }
        except Exception as e:
            print(f"Safety check failed: {e}")
            return {
                "category": "error",
                "danger_potential": 1,
                "safety_info": {"isSafe": True, "confidence": 0.5, "reasoning": f"Safety check failed: {e}", "flags": []}
            }


async def _run_embedding_generation(
    images_list: list,
    text_content: Optional[str],
    context_info: str
) -> Dict[str, Any]:
    """
    Run comprehensive embedding and semantic analysis
    Model: EMBEDDING_MODEL (default: gemini-flash, can use more powerful model)
    """
    prompt = f"""
Analysiere diesen Inhalt für semantische Suche und Knowledge Graph Extraction.
{context_info}

AUFGABEN:
1. CONTENT ANALYSIS - Titel, Untertitel, Tags, Collections
2. KNOWLEDGE GRAPH EXTRACTION - Strukturierte semantische Daten basierend auf Content-Typ
3. EMBEDDING INFO - Rich searchable text und Metadaten

ANTWORT FORMAT (JSON):
{{
  "mediaAnalysis": {{
    "suggestedTitle": "string (max 50 chars)",
    "suggestedSubtitle": "string with emoji",
    "tags": ["tag1", "tag2", "tag3"],
    "collectionSuggestions": ["collection1", "collection2"]
  }},
  "extractedTags": {{
    "keywords": ["keyword1", "keyword2"]
  }},
  "embeddingInfo": {{
    "embeddingText": "Rich searchable text combining all key info",
    "searchableFields": ["field1", "field2"],
    "metadata": {{}}
  }}
}}
"""

    if text_content:
        prompt += f"\n\n--- CONTENT ---\n{text_content}"

    payload = {
        "prompt": {"text": prompt, "images": images_list}
    }

    headers = {
        "X-API-KEY": INTERNAL_API_KEY,
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{API_BASE_URL}/ai/gemini",
                json=payload,
                headers=headers,
                timeout=60.0
            )
            response.raise_for_status()

            ai_response_str = response.json().get("message", "{}")
            ai_response_str = ai_response_str.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(ai_response_str)

            media_analysis = result.get("mediaAnalysis", {})
            extracted_tags = result.get("extractedTags", {})
            embedding_info = result.get("embeddingInfo", {})

            return {
                "ai_title": media_analysis.get("suggestedTitle"),
                "ai_subtitle": media_analysis.get("suggestedSubtitle"),
                "ai_tags": media_analysis.get("tags", []),
                "ai_collections": media_analysis.get("collectionSuggestions", []),
                "extracted_tags": extracted_tags,
                "embedding_info": embedding_info
            }
        except Exception as e:
            print(f"Embedding generation failed: {e}")
            return {
                "ai_title": None,
                "ai_subtitle": None,
                "ai_tags": [],
                "ai_collections": [],
                "extracted_tags": {},
                "embedding_info": {}
            }
