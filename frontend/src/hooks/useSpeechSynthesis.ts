import { useCallback, useEffect, useRef, useState } from "react";

/**
 * useSpeechSynthesis — browser Text-to-Speech (Web Speech API).
 *
 * `speak(messageId, text)` reads text aloud; calling it again with the same
 * message id stops playback (toggle). Tracks which message is being read.
 */
export function useSpeechSynthesis() {
  const [speakingId, setSpeakingId] = useState<number | null>(null);
  const utteranceRef = useRef<SpeechSynthesisUtterance | null>(null);

  const stop = useCallback(() => {
    if (typeof window !== "undefined" && "speechSynthesis" in window) {
      window.speechSynthesis.cancel();
    }
    utteranceRef.current = null;
    setSpeakingId(null);
  }, []);

  const speak = useCallback(
    (messageId: number, text: string) => {
      if (typeof window === "undefined" || !("speechSynthesis" in window)) {
        return;
      }
      // Toggle: if this message is already playing, stop it.
      if (utteranceRef.current && speakingId === messageId) {
        stop();
        return;
      }
      window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(text);
      utteranceRef.current = utterance;
      setSpeakingId(messageId);
      utterance.onend = () => {
        utteranceRef.current = null;
        setSpeakingId(null);
      };
      utterance.onerror = () => {
        utteranceRef.current = null;
        setSpeakingId(null);
      };
      window.speechSynthesis.speak(utterance);
    },
    [speakingId, stop]
  );

  useEffect(() => () => stop(), [stop]);

  return { speakingId, speak, stop };
}
