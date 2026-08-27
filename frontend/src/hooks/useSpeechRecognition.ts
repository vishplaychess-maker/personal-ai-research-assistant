import { useCallback, useEffect, useRef, useState } from "react";

interface SpeechRecognitionResultLike {
  0: { transcript: string };
  isFinal: boolean;
}
interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: ArrayLike<SpeechRecognitionResultLike>;
}
interface SpeechRecognitionErrorLike {
  error?: string;
}
interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onerror: ((e: SpeechRecognitionErrorLike) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}

function createRecognition(): SpeechRecognitionLike | null {
  if (typeof window === "undefined") return null;
  const w = window as unknown as Record<string, unknown>;
  const Ctor = (w.SpeechRecognition || w.webkitSpeechRecognition) as
    | (new () => SpeechRecognitionLike)
    | undefined;
  return Ctor ? new Ctor() : null;
}

/**
 * useSpeechRecognition — browser Speech-to-Text (Web Speech API).
 *
 * Calls `onTranscript(text)` with the live transcript (interim + final)
 * while listening. Exposes listening state, browser support, errors, and
 * start/stop/toggle controls.
 */
export function useSpeechRecognition(
  onTranscript: (text: string) => void
) {
  const [isListening, setIsListening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [supported] = useState<boolean>(() => createRecognition() !== null);
  const recRef = useRef<SpeechRecognitionLike | null>(null);
  const finalRef = useRef("");
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;

  const stop = useCallback(() => {
    const rec = recRef.current;
    if (rec) {
      rec.onend = null;
      try {
        rec.stop();
        rec.abort?.();
      } catch {
        /* ignore */
      }
    }
    recRef.current = null;
    setIsListening(false);
  }, []);

  const start = useCallback(() => {
    const rec = createRecognition();
    if (!rec) {
      setError("Speech recognition is not supported in this browser.");
      return;
    }
    setError(null);
    finalRef.current = "";
    rec.lang = "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    rec.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const res = event.results[i];
        if (res.isFinal) finalRef.current += res[0].transcript + " ";
        else interim += res[0].transcript;
      }
      onTranscriptRef.current(finalRef.current + interim);
    };
    rec.onerror = (event) => {
      const code = event.error || "unknown";
      if (code === "not-allowed" || code === "service-not-allowed") {
        setError("Microphone permission denied. Allow mic access and try again.");
      } else if (code === "no-speech") {
        setError("No speech detected. Try again.");
      } else if (code === "network") {
        setError("Speech recognition network error.");
      } else if (code !== "aborted") {
        setError(`Speech recognition error: ${code}`);
      }
    };
    rec.onend = () => {
      setIsListening(false);
    };
    recRef.current = rec;
    setIsListening(true);
    try {
      rec.start();
    } catch {
      setError("Could not start speech recognition.");
      setIsListening(false);
    }
  }, []);

  const toggle = useCallback(() => {
    if (isListening) stop();
    else start();
  }, [isListening, start, stop]);

  useEffect(() => () => stop(), [stop]);

  return { isListening, supported, error, start, stop, toggle };
}
