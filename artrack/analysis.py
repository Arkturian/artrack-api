import httpx
import json
import base64
import asyncio
import uuid
from typing import Dict, Any, Optional, List
from datetime import datetime
from sqlalchemy.orm import Session

from .config import settings
from .models import AnalysisJob, AnalysisResult, MediaFile, Waypoint
from .database import get_db

class AnalysisService:
    def __init__(self):
        self.ai_base_url = settings.AI_BASE_URL
        self.api_key = settings.AI_API_KEY
        self.timeout = settings.ANALYSIS_TIMEOUT

    async def _call_ai_service(self, endpoint: str, payload: dict) -> dict:
        """Call existing AI service"""
        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json"
        }
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.ai_base_url}{endpoint}",
                json=payload,
                headers=headers
            )
            response.raise_for_status()
            return response.json()

    def _create_outdoor_analysis_prompt(self, media_type: str, user_description: str = "") -> str:
        """Create specialized prompt for outdoor/nature content analysis"""
        
        base_prompt = """
Du bist ein Experte für Outdoor-Aktivitäten und Naturanalyse. 
Analysiere dieses Bild/Medium im Kontext von Wandern, Natur und Outdoor-Aktivitäten.

Analysiere folgende Aspekte:
1. Flora-Identifikation: Erkenne Pflanzen, Bäume, Blumen (besonders heimische Arten)
2. Fauna-Identifikation: Erkenne Tiere, Vögel, Insekten
3. Landschaftsmerkmale: Berge, Wasserfälle, Felsen, Wanderwege, Aussichtspunkte
4. Outdoor-Aktivitäten: Wandern, Klettern, Camping, etc.
5. Wetter/Bedingungen: Sichtweite, Wetterlage
6. Bildqualität: Schärfe, Belichtung, Komposition (Score 0-10)
7. Sicherheitsbewertung: Jugendfreie Inhalte, keine problematischen Inhalte
8. Kategorisierung: Hauptthema des Bildes

Spezielle Aufmerksamkeit für:
- Alpine und subalpine Vegetation
- Typische Wanderinfrastruktur (Wege, Markierungen, Hütten)
- Geologische Formationen und Landschaftstypen
- Outdoor-Equipment und -Aktivitäten
- Naturfotografie-Qualität

Format deine Antwort als JSON mit folgender Struktur:
{
  "description": "Detaillierte Beschreibung was zu sehen ist",
  "categories": ["hauptkategorie", "unterkategorie1", "unterkategorie2"],
  "flora_identification": [
    {"species": "wissenschaftlicher Name", "common_name": "deutscher Name", "confidence": 0.0-1.0}
  ],
  "fauna_identification": [
    {"species": "wissenschaftlicher Name", "common_name": "deutscher Name", "confidence": 0.0-1.0}
  ],
  "landscape_features": ["berg", "wald", "wasserfall", "wanderweg"],
  "outdoor_activities": ["wandern", "bergsteigen", "fotografie"],
  "weather_conditions": ["sonnig", "bewölkt", "neblig"],
  "safety_rating": "safe|warning|unsafe",
  "quality_score": 0.0-10.0,
  "confidence": 0.0-1.0,
  "main_subject": "landschaft|flora|fauna|mensch|aktivität|infrastruktur"
}
"""
        
        if user_description:
            base_prompt += f"\n\nBenutzer-Beschreibung: '{user_description}'"
            base_prompt += "\nBerücksichtige diese Beschreibung in deiner Analyse."
        
        return base_prompt

    async def analyze_image(self, image_base64: str, user_description: str = "") -> dict:
        """Analyze image using existing AI services"""
        
        prompt = self._create_outdoor_analysis_prompt("image", user_description)
        
        payload = {
            "text": prompt,
            "image": f"data:image/jpeg;base64,{image_base64}"
        }
        
        try:
            # Try Gemini first (best for vision tasks)
            response = await self._call_ai_service("/ai/gemini", payload)
            return self._parse_ai_response(response, "image")
        except Exception as e:
            print(f"Gemini analysis failed: {e}")
            try:
                # Fallback to Claude
                response = await self._call_ai_service("/ai/claude", payload)
                return self._parse_ai_response(response, "image")
            except Exception as e2:
                print(f"Claude analysis failed: {e2}")
                # Return basic fallback result
                return self._create_fallback_result("image", user_description)

    async def analyze_audio(self, audio_base64: str, user_description: str = "") -> dict:
        """Analyze audio (placeholder - extend with Whisper integration)"""
        
        # For now, return basic analysis
        # TODO: Integrate with Whisper API for transcription
        return {
            "description": f"Audio-Aufnahme aus Outdoor-Aktivität. {user_description}",
            "categories": ["audio", "natur", "outdoor"],
            "transcription": "",  # Would be filled by Whisper
            "safety_rating": "safe",
            "quality_score": 7.0,
            "confidence": 0.8,
            "main_subject": "audio"
        }

    async def analyze_video(self, video_path: str, user_description: str = "") -> dict:
        """Analyze video (placeholder - extract frames and analyze)"""
        
        # For now, return basic analysis  
        # TODO: Extract frames and analyze with image analysis
        return {
            "description": f"Video-Aufnahme aus Outdoor-Aktivität. {user_description}",
            "categories": ["video", "natur", "outdoor"],
            "safety_rating": "safe",
            "quality_score": 7.0,
            "confidence": 0.8,
            "main_subject": "video"
        }

    def _parse_ai_response(self, response: dict, media_type: str) -> dict:
        """Parse AI response and extract structured data"""
        try:
            # Try to extract JSON from response
            if isinstance(response, dict):
                content = response.get("response", response.get("content", ""))
            else:
                content = str(response)
            
            # Try to find JSON in the response
            start_idx = content.find("{")
            end_idx = content.rfind("}") + 1
            
            if start_idx != -1 and end_idx > start_idx:
                json_str = content[start_idx:end_idx]
                parsed = json.loads(json_str)
                
                # Ensure required fields exist
                return {
                    "description": parsed.get("description", "Outdoor-Aufnahme"),
                    "categories": parsed.get("categories", ["natur", "outdoor"]),
                    "flora_identification": parsed.get("flora_identification", []),
                    "fauna_identification": parsed.get("fauna_identification", []),
                    "landscape_features": parsed.get("landscape_features", []),
                    "outdoor_activities": parsed.get("outdoor_activities", []),
                    "weather_conditions": parsed.get("weather_conditions", []),
                    "safety_rating": parsed.get("safety_rating", "safe"),
                    "quality_score": float(parsed.get("quality_score", 7.0)),
                    "confidence": float(parsed.get("confidence", 0.8)),
                    "main_subject": parsed.get("main_subject", media_type)
                }
            else:
                # Fallback: use the content as description
                return self._create_fallback_result(media_type, content[:500])
                
        except Exception as e:
            print(f"Failed to parse AI response: {e}")
            return self._create_fallback_result(media_type, "")

    def _create_fallback_result(self, media_type: str, description: str = "") -> dict:
        """Create fallback analysis result"""
        return {
            "description": description or f"Outdoor-Aufnahme ({media_type})",
            "categories": [media_type, "natur", "outdoor"],
            "flora_identification": [],
            "fauna_identification": [],
            "landscape_features": [],
            "outdoor_activities": [],
            "weather_conditions": [],
            "safety_rating": "safe",
            "quality_score": 6.0,
            "confidence": 0.5,
            "main_subject": media_type
        }

    async def start_analysis_job(self, media_file_id: int, db: Session) -> str:
        """Start analysis job for media file"""
        
        # Get media file
        media_file = db.query(MediaFile).filter(MediaFile.id == media_file_id).first()
        if not media_file:
            raise ValueError(f"Media file {media_file_id} not found")
        
        # Create job ID
        job_id = str(uuid.uuid4())
        
        # Create analysis job record
        analysis_job = AnalysisJob(
            job_id=job_id,
            media_file_id=media_file_id,
            analysis_type=f"{media_file.media_type}_analysis",
            status="pending"
        )
        
        db.add(analysis_job)
        db.commit()
        
        # Start async analysis
        asyncio.create_task(self._process_analysis_job(job_id, media_file, db))
        
        return job_id

    async def _process_analysis_job(self, job_id: str, media_file: MediaFile, db: Session):
        """Process analysis job asynchronously"""
        
        try:
            # Update job status
            job = db.query(AnalysisJob).filter(AnalysisJob.job_id == job_id).first()
            job.status = "processing"
            job.started_at = datetime.utcnow()
            db.commit()
            
            # Get waypoint for context
            waypoint = db.query(Waypoint).filter(Waypoint.id == media_file.waypoint_id).first()
            user_description = waypoint.user_description if waypoint else ""
            
            # Perform analysis based on media type
            start_time = datetime.utcnow()
            
            if media_file.media_type == "photo":
                # Read image file and convert to base64
                with open(media_file.file_path, "rb") as f:
                    image_data = f.read()
                image_base64 = base64.b64encode(image_data).decode()
                
                analysis_result = await self.analyze_image(image_base64, user_description)
                
            elif media_file.media_type == "audio":
                analysis_result = await self.analyze_audio("", user_description)
                
            elif media_file.media_type == "video":
                analysis_result = await self.analyze_video(media_file.file_path, user_description)
                
            else:
                raise ValueError(f"Unsupported media type: {media_file.media_type}")
            
            # Calculate processing time
            processing_time = (datetime.utcnow() - start_time).total_seconds()
            
            # Save analysis result
            db_analysis_result = AnalysisResult(
                waypoint_id=media_file.waypoint_id,
                media_file_id=media_file.id,
                analysis_job_id=job_id,
                analysis_type=f"{media_file.media_type}_analysis",
                description=analysis_result["description"],
                categories=analysis_result["categories"],
                safety_rating=analysis_result["safety_rating"],
                quality_score=analysis_result["quality_score"],
                confidence=analysis_result["confidence"],
                objects_detected=analysis_result.get("flora_identification", []) + analysis_result.get("fauna_identification", []),
                plant_identification=analysis_result.get("flora_identification", []),
                technical_metrics={
                    "landscape_features": analysis_result.get("landscape_features", []),
                    "outdoor_activities": analysis_result.get("outdoor_activities", []),
                    "weather_conditions": analysis_result.get("weather_conditions", []),
                    "main_subject": analysis_result.get("main_subject", "unknown")
                },
                model_version="artrack-v1.0",
                processing_time_seconds=processing_time
            )
            
            db.add(db_analysis_result)
            
            # Update media file status
            media_file.processing_state = "published"
            
            # Update waypoint processing state
            if waypoint:
                # Check if all media files for this waypoint are processed
                pending_media = db.query(MediaFile).filter(
                    MediaFile.waypoint_id == waypoint.id,
                    MediaFile.processing_state.in_(["pending", "uploading", "uploaded", "analysing"])
                ).count()
                
                if pending_media == 0:
                    waypoint.processing_state = "published"
                    waypoint.moderation_status = "approved" if analysis_result["safety_rating"] == "safe" else "pending"
            
            # Update job status
            job.status = "completed"
            job.completed_at = datetime.utcnow()
            
            db.commit()
            
        except Exception as e:
            # Handle errors
            print(f"Analysis job {job_id} failed: {e}")
            
            job = db.query(AnalysisJob).filter(AnalysisJob.job_id == job_id).first()
            job.status = "failed"
            job.error_message = str(e)
            job.completed_at = datetime.utcnow()
            
            # Update media file status
            media_file.processing_state = "failed"
            
            db.commit()

# Global analysis service instance
analysis_service = AnalysisService()