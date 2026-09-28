using System.Collections;
using System.Collections.Generic;
using UnityEngine;

[ExecuteAlways]
public class CustomAudioSource : MonoBehaviour
{
    public AudioFixture audioFixture;    
    
    public float delayInMeters;
    public float delayInMilliseconds;
    public bool reversePolarity=false;

    float oldDelayInMeters;
    float oldDelayInMilliseconds;

    public Array parentArray;

    public bool muted;
    public bool isSolo;
    public float attenuation;

    Vector3 frustumPointNTL;
    Vector3 frustumPointNTR;
    Vector3 frustumPointNBL;
    Vector3 frustumPointNBR;
    Vector3 frustumPointFTL;
    Vector3 frustumPointFTR;
    Vector3 frustumPointFBL;
    Vector3 frustumPointFBR;    

    // Start is called before the first frame update
    void Start()
    {
        oldDelayInMeters=delayInMeters;
        oldDelayInMilliseconds=delayInMilliseconds;
    }
    
    public void Update(){

        //permette di cambiare uno qualunque dei due campi, e corregge l'altro di conseguenza
        if (oldDelayInMeters!=delayInMeters){
            oldDelayInMeters=delayInMeters;
            delayInMilliseconds=delayInMeters*1000f/343f;
            oldDelayInMilliseconds=delayInMilliseconds;
        }
        if (oldDelayInMilliseconds!=delayInMilliseconds){
            oldDelayInMilliseconds=delayInMilliseconds;
            delayInMeters=delayInMilliseconds*343f/1000f;
            oldDelayInMeters=delayInMeters;
        }

        //update frustum points
        float farPlaneDistance = 10;

        float halfVerticalFOV = Mathf.Tan(Mathf.Deg2Rad * audioFixture.verticalDispersion / 2.0f);
        float halfHorizontalFOV = Mathf.Tan(Mathf.Deg2Rad * audioFixture.horizontalDispersion / 2.0f);

        frustumPointNTL = new Vector3(-audioFixture.dispersionPlaneSize.x/2, audioFixture.dispersionPlaneSize.y/2, audioFixture.nearDispersionPlaneDistance);
        frustumPointNTR = new Vector3(audioFixture.dispersionPlaneSize.x/2, audioFixture.dispersionPlaneSize.y/2, audioFixture.nearDispersionPlaneDistance);
        frustumPointNBL = new Vector3(-audioFixture.dispersionPlaneSize.x/2, -audioFixture.dispersionPlaneSize.y/2, audioFixture.nearDispersionPlaneDistance);
        frustumPointNBR = new Vector3(audioFixture.dispersionPlaneSize.x/2, -audioFixture.dispersionPlaneSize.y/2, audioFixture.nearDispersionPlaneDistance);

        frustumPointFTL = new Vector3(-halfHorizontalFOV*farPlaneDistance, halfVerticalFOV*farPlaneDistance, farPlaneDistance);                
        frustumPointFTR = new Vector3(halfHorizontalFOV*farPlaneDistance, halfVerticalFOV*farPlaneDistance, farPlaneDistance);        
        frustumPointFBL = new Vector3(-halfHorizontalFOV*farPlaneDistance, -halfVerticalFOV*farPlaneDistance, farPlaneDistance);
        frustumPointFBR = new Vector3(halfHorizontalFOV*farPlaneDistance, -halfVerticalFOV*farPlaneDistance, farPlaneDistance);

        //move frustum points accordind to object transform
        Matrix4x4 matrix = Matrix4x4.TRS(transform.position,transform.rotation,Vector3.one);
        frustumPointNTL = matrix * new Vector4(frustumPointNTL.x,frustumPointNTL.y,frustumPointNTL.z,1.0f);
        frustumPointNTR = matrix * new Vector4(frustumPointNTR.x,frustumPointNTR.y,frustumPointNTR.z,1.0f);
        frustumPointNBL = matrix * new Vector4(frustumPointNBL.x,frustumPointNBL.y,frustumPointNBL.z,1.0f);
        frustumPointNBR = matrix * new Vector4(frustumPointNBR.x,frustumPointNBR.y,frustumPointNBR.z,1.0f);

        frustumPointFTL = matrix * new Vector4(frustumPointFTL.x,frustumPointFTL.y,frustumPointFTL.z,1.0f);
        frustumPointFTR = matrix * new Vector4(frustumPointFTR.x,frustumPointFTR.y,frustumPointFTR.z,1.0f);
        frustumPointFBL = matrix * new Vector4(frustumPointFBL.x,frustumPointFBL.y,frustumPointFBL.z,1.0f);
        frustumPointFBR = matrix * new Vector4(frustumPointFBR.x,frustumPointFBR.y,frustumPointFBR.z,1.0f);
    }

    //order of Planes -> Left,Right,Top, Bottom, Near
    public Plane[] GetDispersionFrustumPlanesLRTBN(){        
        Plane[] planes = new Plane[5];
        planes[0] = new Plane(frustumPointFTL, frustumPointNTL, frustumPointNBL);
        planes[1] = new Plane(frustumPointNTR, frustumPointFTR, frustumPointFBR);
        planes[2] = new Plane(frustumPointNTL, frustumPointFTL, frustumPointFTR);
        planes[3] = new Plane(frustumPointNBR, frustumPointFBR, frustumPointFBL);
        planes[4] = new Plane(frustumPointNTL, frustumPointNTR, frustumPointNBR);        
        return planes;
    }

    void OnDrawGizmoss(){
        //draw forward axis
        Gizmos.color=Color.blue;
        Gizmos.DrawLine(transform.position,transform.position+transform.forward*transform.localScale.z);
        //draw dispersionFrustum        
        Gizmos.DrawLine(frustumPointNTL, frustumPointNTR);
        Gizmos.DrawLine(frustumPointNTR, frustumPointNBR);
        Gizmos.DrawLine(frustumPointNBR, frustumPointNBL);
        Gizmos.DrawLine(frustumPointNBL, frustumPointNTL);

        Gizmos.DrawLine(frustumPointFTL, frustumPointFTR);
        Gizmos.DrawLine(frustumPointFTR, frustumPointFBR);
        Gizmos.DrawLine(frustumPointFBR, frustumPointFBL);
        Gizmos.DrawLine(frustumPointFBL, frustumPointFTL);

        Gizmos.DrawLine(frustumPointNTL, frustumPointFTL);
        Gizmos.DrawLine(frustumPointNTR, frustumPointFTR);
        Gizmos.DrawLine(frustumPointNBR, frustumPointFBR);
        Gizmos.DrawLine(frustumPointNBL, frustumPointFBL);
    }
}
