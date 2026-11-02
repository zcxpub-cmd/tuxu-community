import { ArrowLeft, ArrowRight, ArrowsClockwise, BookOpen, Check, DownloadSimple, FileText, GearSix, HouseSimple, ImageSquare, Plus, ShieldCheck, Sparkle, Trash, UploadSimple, X } from '@phosphor-icons/react'

const icons = { plus: Plus, arrow: ArrowRight, back: ArrowLeft, upload: UploadSimple, file: FileText, spark: Sparkle, check: Check, refresh: ArrowsClockwise, trash: Trash, image: ImageSquare, shield: ShieldCheck, download: DownloadSimple, close: X, book: BookOpen, home: HouseSimple, settings: GearSix }
export default function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const Symbol = icons[name as keyof typeof icons] || FileText
  return <Symbol size={size} weight="light" aria-hidden="true" />
}
