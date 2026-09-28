using System.Collections.Generic;
using System.Linq;
using UnityEngine;

public class AudioManager : MonoBehaviour
{
    public List<CustomAudioSource> audioSources;

    public Material material;

    public float frequency=100;
    public float soundSpeedInMetersPerSecond=343;
    public float maxDisplayedSpl;    
    public bool displayGradient;
    public float gradientChunkSizeInDB;

    public Color[] gradientColors = new Color[10];

    public Texture2D audioDataTexture;

    //define the vertical size of the data Texture
    int numberOfParametersPassed=27;

    ClearSolo clearSolo;

    void Start(){
        audioSources= new List<CustomAudioSource>();
        clearSolo=FindObjectOfType<ClearSolo>();
    }

    // Update is called once per frame
    void Update()
    {        
        audioSources= GameObject.FindObjectsOfType<CustomAudioSource>().ToList();
        material.SetFloat("_maxSPLDisplayed",maxDisplayedSpl);
        material.SetInt("_gradientDisplay",displayGradient?1:0);
        material.SetFloat("_frequency",frequency);
        material.SetFloat("_soundSpeedInMetersPerSecond",soundSpeedInMetersPerSecond);

        material.SetInt("_audioSourcesCount",audioSources.Count);        
        
        material.SetColorArray("_gradientColors", gradientColors);
        material.SetFloat("_gradientDiscreteChangeInDb", gradientChunkSizeInDB);
        material.SetInt("_textureVerticalSize", numberOfParametersPassed);
       
        
        ReloadAudioDataTexture();
    }

    void ReloadAudioDataTexture(){
        if (audioSources.Count==0){
            return;
        }

        // This is a bit inefficient, but it works for now.
        // We could make it more efficient by only rewriting the data when it changes.                
        audioDataTexture = new Texture2D(audioSources.Count,numberOfParametersPassed,TextureFormat.RFloat,false);
        audioDataTexture.filterMode=FilterMode.Point;

        float[] audioDataFloats = new float[audioSources.Count*numberOfParametersPassed];
        for (int i=0; i<audioSources.Count;i++){
            audioDataFloats[i] = audioSources[i].transform.position.x;
            audioDataFloats[i+audioSources.Count] = audioSources[i].transform.position.y;
            audioDataFloats[i+audioSources.Count*2] = audioSources[i].transform.position.z;
            bool muted;
            if (clearSolo.activeSolos.Count==0){
                muted = audioSources[i].muted;
                if (audioSources[i].parentArray!=null){
                    if (audioSources[i].parentArray.muted){
                        muted=true;
                    }
                }
            }
            else{
                muted=!audioSources[i].isSolo;
                if (audioSources[i].parentArray!=null){
                    if (audioSources[i].parentArray.isSolo){
                        muted=false;
                    }
                }
            }
            if (muted){
                audioDataFloats[i+audioSources.Count*3] = float.MinValue;
            }
            else{
                float attenuation=audioSources[i].attenuation;
                if (audioSources[i].parentArray!=null){
                    attenuation+=audioSources[i].parentArray.attenuation;
                }                
                audioDataFloats[i+audioSources.Count*3] = audioSources[i].GetComponent<CustomAudioSource>().audioFixture.dbAtOneMeter+attenuation;
            }
            float delayInMeters = audioSources[i].GetComponent<CustomAudioSource>().delayInMeters;
            bool reversePolarity = audioSources[i].GetComponent<CustomAudioSource>().reversePolarity;
            Array parentArray = audioSources[i].GetComponent<CustomAudioSource>().parentArray;
            if (parentArray!=null){
                delayInMeters+=parentArray.delayInMeters;
                reversePolarity=reversePolarity^parentArray.reversePolarity;
            }
            audioDataFloats[i+audioSources.Count*4] = delayInMeters;
            audioDataFloats[i+audioSources.Count*5] = reversePolarity?-1:1;

            //pass dispersion frustum planes
            if (audioSources[i].audioFixture.forceOmnidirectional){
                audioDataFloats[i+audioSources.Count*6] = 1;
            }
            else{
                audioDataFloats[i+audioSources.Count*6] = 0;
                Plane[] planes=audioSources[i].GetDispersionFrustumPlanesLRTBN();
                //Left Frustum Plane (normal.x, normal.y, normal.z, distanceFromOrigin)
                audioDataFloats[i+audioSources.Count*7] = planes[0].normal.x;
                audioDataFloats[i+audioSources.Count*8] = planes[0].normal.y;
                audioDataFloats[i+audioSources.Count*9] = planes[0].normal.z;
                audioDataFloats[i+audioSources.Count*10] = planes[0].distance;
                //Right Frustum Plane (normal.x, normal.y, normal.z, distanceFromOrigin)
                audioDataFloats[i+audioSources.Count*11] = planes[1].normal.x;
                audioDataFloats[i+audioSources.Count*12] = planes[1].normal.y;
                audioDataFloats[i+audioSources.Count*13] = planes[1].normal.z;
                audioDataFloats[i+audioSources.Count*14] = planes[1].distance;
                //Top Frustum Plane (normal.x, normal.y, normal.z, distanceFromOrigin)
                audioDataFloats[i+audioSources.Count*15] = planes[2].normal.x;
                audioDataFloats[i+audioSources.Count*16] = planes[2].normal.y;
                audioDataFloats[i+audioSources.Count*17] = planes[2].normal.z;
                audioDataFloats[i+audioSources.Count*18] = planes[2].distance;
                //Bottom Frustum Plane (normal.x, normal.y, normal.z, distanceFromOrigin)
                audioDataFloats[i+audioSources.Count*19] = planes[3].normal.x;
                audioDataFloats[i+audioSources.Count*20] = planes[3].normal.y;
                audioDataFloats[i+audioSources.Count*21] = planes[3].normal.z;
                audioDataFloats[i+audioSources.Count*22] = planes[3].distance;
                //Near Frustum PLane (normal.x, normal.y, normal.z, distanceFromOrigin)
                audioDataFloats[i+audioSources.Count*23] = planes[4].normal.x;
                audioDataFloats[i+audioSources.Count*24] = planes[4].normal.y;
                audioDataFloats[i+audioSources.Count*25] = planes[4].normal.z;
                audioDataFloats[i+audioSources.Count*26] = planes[4].distance;
            }
        }

        audioDataTexture.SetPixelData(audioDataFloats,0);
        audioDataTexture.Apply();
        material.SetTexture("_audioDataTexture",audioDataTexture);
    }
}
