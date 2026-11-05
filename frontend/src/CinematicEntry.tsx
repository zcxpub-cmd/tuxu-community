import { t } from './i18n'
import LanguageSwitch from './LanguageSwitch'
import { useEffect, useRef, useState } from 'react'
import gsap from 'gsap'
import { useGSAP } from '@gsap/react'
import Icon from './Icons'
import type { Scene } from './sceneLibrary'

gsap.registerPlugin(useGSAP)

export default function CinematicEntry({ scene, motionEnabled, onStart, onStudio, onResume, onSettings }: {
  scene: Scene; motionEnabled: boolean; onStart: () => void; onStudio: () => void; onResume: () => void; onSettings: () => void
}) {
  const root = useRef<HTMLElement>(null)
  const video = useRef<HTMLVideoElement>(null)
  const [reducedMotion, setReducedMotion] = useState(() => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  const [failedVideo, setFailedVideo] = useState<string | null>(null)
  const videoActive = !!scene.videoUrl && motionEnabled && !reducedMotion && failedVideo !== scene.id
  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const change = () => setReducedMotion(query.matches)
    query.addEventListener('change', change); change()
    return () => query.removeEventListener('change', change)
  }, [])
  useEffect(() => {
    const element = video.current
    if (!videoActive || !element) return
    let frame: number | undefined
    const cancelFrame = () => {
      if (frame !== undefined) cancelAnimationFrame(frame)
      frame = undefined
    }
    const paintFade = () => {
      const blend = Math.min(element.currentTime / .28, (element.duration - element.currentTime) / .6)
      const opacity = Number.isFinite(blend) ? Math.min(1, Math.max(0, blend)) : 0
      element.style.opacity = String(opacity * opacity * (3 - 2 * opacity))
    }
    const renderFrame = () => {
      frame = undefined
      if (document.hidden || element.paused) return
      paintFade()
      frame = requestAnimationFrame(renderFrame)
    }
    const playing = () => {
      if (document.hidden) element.pause()
      else if (scene.fadeLoop && frame === undefined) renderFrame()
    }
    const update = () => {
      if (document.hidden) {
        cancelFrame()
        element.pause()
      } else {
        void element.play().catch(() => { /* The still poster remains available if autoplay is blocked. */ })
        if (!element.paused && element.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) playing()
      }
    }
    element.addEventListener('playing', playing)
    if (scene.fadeLoop) {
      element.addEventListener('pause', cancelFrame)
      element.addEventListener('waiting', cancelFrame)
      // Native looping seeks back to the first frame. Reveal the matching poster during that seek.
      element.addEventListener('seeking', paintFade)
    }
    document.addEventListener('visibilitychange', update); update()
    return () => {
      document.removeEventListener('visibilitychange', update)
      element.removeEventListener('playing', playing)
      element.removeEventListener('pause', cancelFrame)
      element.removeEventListener('waiting', cancelFrame)
      element.removeEventListener('seeking', paintFade)
      cancelFrame()
      element.pause()
    }
  }, [videoActive, scene.id, scene.fadeLoop])
  useGSAP(() => {
    const media = gsap.matchMedia()
    media.add('(prefers-reduced-motion: no-preference)', () => {
      if (!motionEnabled) return
      const camera = !videoActive ? gsap.fromTo('.entry-backdrop', { scale: 1.035, xPercent: -.35 },
        { scale: 1.075, xPercent: .35, duration: 24, repeat: -1, yoyo: true, ease: 'sine.inOut' }) : null
      gsap.from('.entry-copy', { y: 12, opacity: .7, duration: .6, ease: 'power2.out', clearProps: 'opacity,transform' })
      const visibility = () => { camera?.paused(document.hidden) }
      document.addEventListener('visibilitychange', visibility); visibility()
      const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)')
      const x = gsap.quickTo('.entry-parallax', 'x', { duration: 1.4, ease: 'power2.out' })
      const y = gsap.quickTo('.entry-parallax', 'y', { duration: 1.4, ease: 'power2.out' })
      const pointer = (event: PointerEvent) => {
        if (videoActive || !finePointer.matches || document.hidden) return
        x((event.clientX / window.innerWidth - .5) * 8)
        y((event.clientY / window.innerHeight - .5) * 6)
      }
      const leave = () => { x(0); y(0) }
      const element = root.current
      element?.addEventListener('pointermove', pointer); element?.addEventListener('pointerleave', leave)
      return () => {
        document.removeEventListener('visibilitychange', visibility)
        element?.removeEventListener('pointermove', pointer); element?.removeEventListener('pointerleave', leave)
      }
    }, root)
    return () => media.revert()
  }, { scope: root, dependencies: [motionEnabled, scene.id, videoActive], revertOnUpdate: true })

  return <main ref={root} className="cinematic-entry" data-scene={scene.id} aria-label={t("图序首页")}>
    <div className="entry-parallax" aria-hidden="true">
      <img className="entry-backdrop" src={scene.url} alt="" fetchPriority="high" />
      {videoActive && <video key={scene.id} ref={video}
        className={`entry-backdrop entry-video${scene.fadeLoop ? ' entry-video-fade' : ''}`}
        src={scene.videoUrl} poster={scene.url} muted autoPlay loop playsInline preload="auto"
        onError={() => setFailedVideo(scene.id)} />}
    </div>
    <div className="entry-shade" aria-hidden="true" />
    <nav className="entry-nav" aria-label={t("主导航")}>
      <div className="entry-brand"><span className="tuxu-brand-mark" aria-hidden="true" /><span>{t("图序")}</span><small>TUXU</small></div>
      <div className="entry-links"><LanguageSwitch /><button onClick={onStudio}>{t("工作台")}</button><button onClick={onResume}>{t("作品记录")}</button><button onClick={onSettings} title={t("设置，包含界面动效开关")}>{t("设置")}</button></div>
    </nav>
    <div className="entry-main"><div className="entry-copy">
      <h1 className="entry-title">{t("让文字，")}<br />{t("拥有色彩。")}</h1>
      <p className="entry-description">{t("从一篇文章，开始一场视觉叙事。")}</p>
      <div className="entry-actions"><button className="entry-primary" onClick={onStart}>{t("开始创作")}{' '}<Icon name="arrow" size={25} /></button><button className="entry-secondary" onClick={onResume}>{t("继续项目")}</button></div>
      <p className="entry-principles">{t("本地分析")}{' '}<span>·</span>{' '}{t("自由构图")}{' '}<span>·</span>{' '}{t("确认后生成")}</p>
    </div></div>
    <div className="entry-flow" aria-label={t("从文章到成稿")}><span>{t("文章")}</span><i>/</i><span>{t("画面")}</span><i>/</i><span>{t("成稿")}</span></div>
  </main>
}
